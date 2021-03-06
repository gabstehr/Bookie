"""View callables for utilities like bookmark imports, etc"""
import logging
import os
import random
import string

from pyramid.httpexceptions import HTTPFound
from pyramid.httpexceptions import HTTPNotFound
from pyramid.view import view_config
from sqlalchemy.orm import contains_eager

from bookie.lib.access import ReqAuthorize
from bookie.lib.applog import BmarkLog

from bookie.bcelery import tasks
from bookie.models import Bmark
from bookie.models import DBSession
from bookie.models import Hashed
from bookie.models.fulltext import get_fulltext_handler
from bookie.models.queue import NEW
from bookie.models.queue import ImportQueue
from bookie.models.queue import ImportQueueMgr


LOG = logging.getLogger(__name__)


@view_config(route_name="user_import", renderer="/utils/import.mako")
def import_bmarks(request):
    """Allow users to upload a delicious bookmark export"""
    rdict = request.matchdict
    username = rdict.get('username')

    # if auth fails, it'll raise an HTTPForbidden exception
    with ReqAuthorize(request):
        data = {}
        post = request.POST

        # we can't let them submit multiple times, check if this user has an
        # import in process
        if ImportQueueMgr.get(username=username, status=NEW):
            # they have an import, get the information about it and shoot to
            # the template
            return {
                'existing': True,
                'import_stats': ImportQueueMgr.get_details(username=username)
            }

        if post:
            # we have some posted values
            files = post.get('import_file', None)

            if files is not None:
                # save the file off to the temp storage
                out_dir = "{storage_dir}/{randdir}".format(
                    storage_dir=request.registry.settings.get(
                        'import_files',
                        '/tmp/bookie').format(
                            here=request.registry.settings.get('app_root')),
                    randdir=random.choice(string.letters),
                )

                # make sure the directory exists
                # we create it with parents as well just in case
                if not os.path.isdir(out_dir):
                    os.makedirs(out_dir)

                out_fname = "{0}/{1}.{2}".format(
                    out_dir, username, files.filename)
                out = open(out_fname, 'w')
                out.write(files.file.read())
                out.close()

                # mark the system that there's a pending import that needs to
                # be completed
                q = ImportQueue(username, out_fname)
                DBSession.add(q)
                DBSession.flush()
                # Schedule a task to start this import job.
                tasks.importer_process.delay(q.id)

                return HTTPFound(location=request.route_url('user_import',
                                                            username=username))
            else:
                msg = request.session.pop_flash()
                if msg:
                    data['error'] = msg
                else:
                    data['error'] = None

            return data
        else:
            # we need to see if they've got
            # just display the form
            return {
                'existing': False
            }


@view_config(route_name="search", renderer="/utils/search.mako")
@view_config(route_name="user_search", renderer="/utils/search.mako")
def search(request):
    """Display the search form to the user"""
    # if this is a url /username/search then we need to update the search form
    # action to /username/results
    rdict = request.matchdict
    username = rdict.get('username', None)
    return {'username': username}


@view_config(route_name="search_results",
             renderer="/utils/results_wrap.mako")
@view_config(route_name="user_search_results",
             renderer="/utils/results_wrap.mako")
@view_config(route_name="search_results_ajax", renderer="json")
@view_config(route_name="user_search_results_ajax", renderer="json")
@view_config(route_name="search_results_rest",
             renderer="/utils/results_wrap.mako")
@view_config(route_name="user_search_results_rest",
             renderer="/utils/results_wrap.mako")
def search_results(request):
    """Search for the query terms in the matchdict/GET params

    The ones in the matchdict win in the case that they both exist
    but we'll fall back to query string search=XXX

    """
    route_name = request.matched_route.name
    mdict = request.matchdict
    rdict = request.GET

    username = rdict.get('username', None)

    if 'terms' in mdict:
        phrase = " ".join(mdict['terms'])
    else:
        phrase = rdict.get('search', '')

    # Always search the fulltext content
    with_content = True

    conn_str = request.registry.settings.get('sqlalchemy.url', False)
    searcher = get_fulltext_handler(conn_str)

    # check if we have a page count submitted
    params = request.params
    page = params.get('page', 0)
    count = params.get('count', 50)

    res_list = searcher.search(phrase,
                               content=with_content,
                               username=username,
                               ct=count,
                               page=page,
                               )

    # if the route name is search_ajax we want a json response
    # else we just want to return the payload data to the mako template
    if 'ajax' in route_name or 'api' in route_name:
        return {
            'success': True,
            'message': "",
            'payload': {
                'search_results': [dict(res) for res in res_list],
                'result_count': len(res_list),
                'phrase': phrase,
                'page': page,
                'username': username,
            }
        }
    else:
        return {
            'search_results': res_list,
            'count': len(res_list),
            'max_count': 50,
            'phrase': phrase,
            'page': page,
            'username': username,
        }


@view_config(route_name="user_export", renderer="/utils/export.mako")
def export(request):
    """Handle exporting a user's bookmarks to file"""
    rdict = request.matchdict
    username = rdict.get('username')

    if request.user is not None:
        current_user = request.user.username
    else:
        current_user = None

    bmark_list = Bmark.query.join(Bmark.tags).\
        options(
            contains_eager(Bmark.tags)
        ).\
        join(Bmark.hashed).\
        options(
            contains_eager(Bmark.hashed)
        ).\
        filter(Bmark.username == username).all()

    BmarkLog.export(username, current_user)

    request.response_content_type = 'text/html'

    headers = [('Content-Disposition',
                'attachment; filename="bookie_export.html"')]
    setattr(request, 'response_headerlist', headers)

    return {
        'bmark_list': bmark_list,
    }


@view_config(route_name="redirect", renderer="/utils/redirect.mako")
@view_config(route_name="user_redirect", renderer="/utils/redirect.mako")
def redirect(request):
    """Handle redirecting to the selected url

    We want to increment the clicks counter on the bookmark url here

    """
    rdict = request.matchdict
    hash_id = rdict.get('hash_id', None)
    username = rdict.get('username', None)

    hashed = Hashed.query.get(hash_id)

    if not hashed:
        # for some reason bad link, 404
        return HTTPNotFound()

    hashed.clicks = hashed.clicks + 1

    if username is not None:
        bookmark = Bmark.query.\
            filter(Bmark.hash_id == hash_id).\
            filter(Bmark.username == username).one()
        bookmark.clicks = bookmark.clicks + 1

    return HTTPFound(location=hashed.url)
