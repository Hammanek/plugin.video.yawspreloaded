import io
import os
import sys
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs
import requests
from xml.etree import ElementTree as ET
import hashlib
import zlib
import struct
from md5crypt import md5crypt
import traceback
import json
import unidecode
import time
from datetime import date
import re
import zipfile
import uuid
from functools import wraps
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

try:
    from urllib import urlencode
    from urlparse import parse_qsl, urlparse
except ImportError:
    from urllib.parse import urlencode
    from urllib.parse import parse_qsl, urlparse

try:
    from xbmc import translatePath
except ImportError:
    from xbmcvfs import translatePath

# Constants
BASE = 'https://webshare.cz'
API = BASE + '/api/'
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0"
HEADERS = {'User-Agent': UA, 'Referer': BASE}
REALM = ':Webshare:'
CATEGORIES = ['', 'video', 'images', 'audio', 'archives', 'docs', 'adult']
SORTS = ['', 'recent', 'rating', 'largest', 'smallest']
SEARCH_HISTORY = 'search_history'
NONE_WHAT = '%#NONE#%'
BACKUP_DB = 'D1iIcURxlR'

# TMDB API constants
TMDB_API_URL = 'https://api.themoviedb.org'
TMDB_IMAGE_URL = 'https://image.tmdb.org/t/p/'
TMDB_AUTH_URL = 'https://www.themoviedb.org/authenticate/'
TMDB_TRAILERS_FILE = 'tmdb_trailers.json'
TMDB_SHOWS_FILE = 'tmdb_shows.json'
TMDB_SHOWS_TTL = 86400
_TMDB_GENRES_CACHE = {}
_TMDB_TRAILERS_CACHE = {}
_TMDB_TRAILERS_LOADED = False
_TMDB_SHOWS_CACHE = {}
_TMDB_SHOWS_LOADED = False

# Plugin setup
_url = sys.argv[0]
_handle = int(sys.argv[1])
_addon = xbmcaddon.Addon()
_session = requests.Session()
_session.headers.update(HEADERS)
_profile = translatePath(_addon.getAddonInfo('profile'))

try:
    _profile = _profile.decode("utf-8")
except AttributeError:
    pass

def log(message, level=xbmc.LOGDEBUG):
    xbmc.log(f"[{_addon.getAddonInfo('id')}] {message}", level)

def handle_errors(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            log(f"Error in {func.__name__}: {str(e)}", xbmc.LOGERROR)
            traceback.print_exc()
            popinfo(_addon.getLocalizedString(30102), icon=xbmcgui.NOTIFICATION_ERROR, sound=True)
            return None
    return wrapper

def get_url(**kwargs):
    return f'{_url}?{urlencode(kwargs)}'

@handle_errors
def api(fnct, data):
    try:
        response = _session.post(API + fnct + "/", data=data, timeout=30)
        response.raise_for_status()
        return response
    except requests.RequestException as e:
        log(f"API request failed: {str(e)}", xbmc.LOGERROR)
        raise

def is_ok(xml):
    status = xml.find('status')
    return status is not None and status.text == 'OK'

def popinfo(message, heading=_addon.getAddonInfo('name'), icon=xbmcgui.NOTIFICATION_INFO, time=3000, sound=False):
    xbmcgui.Dialog().notification(heading, message, icon, time, sound=sound)

@handle_errors
def login():
    username = _addon.getSetting('wsuser')
    password = _addon.getSetting('wspass')
    if username == '' or password == '':
        popinfo(_addon.getLocalizedString(30101), sound=True)
        _addon.openSettings()
        return
    response = api('salt', {'username_or_email': username})
    xml = ET.fromstring(response.content)
    if is_ok(xml):
        salt = xml.find('salt').text
        try:
            encrypted_pass = hashlib.sha1(md5crypt(password.encode('utf-8'), salt.encode('utf-8'))).hexdigest()
            pass_digest = hashlib.md5(username.encode('utf-8') + REALM + encrypted_pass.encode('utf-8')).hexdigest()
        except TypeError:
            encrypted_pass = hashlib.sha1(md5crypt(password.encode('utf-8'), salt.encode('utf-8')).encode('utf-8')).hexdigest()
            pass_digest = hashlib.md5(username.encode('utf-8') + REALM.encode('utf-8') + encrypted_pass.encode('utf-8')).hexdigest()
        response = api('login', {'username_or_email': username, 'password': encrypted_pass, 'digest': pass_digest, 'keep_logged_in': 1})
        xml = ET.fromstring(response.content)
        if is_ok(xml):
            token = xml.find('token').text
            _addon.setSetting('token', token)
            return token
        else:
            popinfo(_addon.getLocalizedString(30102), icon=xbmcgui.NOTIFICATION_ERROR, sound=True)
            _addon.openSettings()
    else:
        popinfo(_addon.getLocalizedString(30102), icon=xbmcgui.NOTIFICATION_ERROR, sound=True)
        _addon.openSettings()

def revalidate():
    token = _addon.getSetting('token')
    if not token:
        return login()
    
    response = api('user_data', {'wst': token})
    xml = ET.fromstring(response.content)
    
    if is_ok(xml):
        vip = xml.find('vip').text
        if vip != '1':
            popinfo(_addon.getLocalizedString(30103), icon=xbmcgui.NOTIFICATION_WARNING)
        return token
    else:
        _addon.setSetting('token', '')
        return login()

def todict(xml, skip=None):
    if skip is None:
        skip = []
    result = {}
    for e in xml:
        if e.tag not in skip:
            value = e.text if len(list(e)) == 0 else todict(e, skip)
            if e.tag in result:
                if isinstance(result[e.tag], list):
                    result[e.tag].append(value)
                else:
                    result[e.tag] = [result[e.tag], value]
            else:
                result[e.tag] = value
    return result

def sizelize(txtsize, units=None):
    if units is None:
        units = ['B', 'KB', 'MB', 'GB']
    if not txtsize:
        return str(txtsize)
    
    try:
        size = float(txtsize)
        for unit in units[:-1]:
            if size < 1024.0:
                return f"{size:.0f}{unit}" if unit == 'B' else f"{size:.2f}{unit}"
            size /= 1024.0
        return f"{size:.2f}{units[-1]}"
    except (ValueError, TypeError):
        return str(txtsize)

def labelize(file):
    size = sizelize(file.get('size') or file.get('sizelized', '?'))
    return f"{file['name']} ({size})"

def tolistitem(file, addcommands=None):
    if addcommands is None:
        addcommands = []
    label = labelize(file)
    listitem = xbmcgui.ListItem(label=label)
    
    if 'img' in file:
        listitem.setArt({'thumb': file['img']})
    
    listitem.setInfo('video', {'title': label})
    listitem.setProperty('IsPlayable', 'true')
    
    commands = [
        (_addon.getLocalizedString(30211), f'RunPlugin({get_url(action="info", ident=file["ident"])})'),
        (_addon.getLocalizedString(30212), f'RunPlugin({get_url(action="download", ident=file["ident"])})')
    ]
    commands.extend(addcommands)
    listitem.addContextMenuItems(commands)
    return listitem

def ask(what=None):
    what = what or ''
    kb = xbmc.Keyboard(what, _addon.getLocalizedString(30007))
    kb.doModal()
    return kb.getText() if kb.isConfirmed() else None

def loadsearch():
    history = []
    history_path = os.path.join(_profile, SEARCH_HISTORY)
    
    try:
        os.makedirs(_profile, exist_ok=True)
        if os.path.exists(history_path):
            with io.open(history_path, 'r', encoding='utf-8') as file:
                history = json.load(file)
    except Exception as e:
        log(f"Error loading search history: {str(e)}", xbmc.LOGERROR)
    
    return history

def storesearch(what):
    if not what:
        return
    
    try:
        history = loadsearch()
        if what in history:
            history.remove(what)
        
        history.insert(0, what)
        history = history[:int(_addon.getSetting('shistory'))]
        
        with io.open(os.path.join(_profile, SEARCH_HISTORY), 'w', encoding='utf-8') as file:
            json.dump(history, file)
    except Exception as e:
        log(f"Error storing search: {str(e)}", xbmc.LOGERROR)

def removesearch(what):
    if not what:
        return
    
    try:
        history = loadsearch()
        if what in history:
            history.remove(what)
            with io.open(os.path.join(_profile, SEARCH_HISTORY), 'w', encoding='utf-8') as file:
                json.dump(history, file)
    except Exception as e:
        log(f"Error removing search: {str(e)}", xbmc.LOGERROR)

def dosearch(token, what, category, sort, limit, offset, action):
    params = {
        'what': '' if what == NONE_WHAT else what,
        'category': category,
        'sort': sort,
        'limit': limit,
        'offset': offset,
        'wst': token,
        'maybe_removed': 'true'
    }

    what = normalize_season_episode(what)

    log(f"Searching for: {what}", xbmc.LOGERROR)
    
    response = api('search', params)
    xml = ET.fromstring(response.content)
    
    if not is_ok(xml):
        popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)
        return
    
    # Previous page
    if offset > 0:
        listitem = xbmcgui.ListItem(label=_addon.getLocalizedString(30206))
        listitem.setArt({'icon': 'DefaultAddonsSearch.png'})
        xbmcplugin.addDirectoryItem(
            _handle,
            get_url(
                action=action,
                what=what,
                category=category,
                sort=sort,
                limit=limit,
                offset=max(0, offset - limit)
            ),
            listitem,
            True
        )
    
    # Files
    for file in xml.iter('file'):
        item = todict(file)
        
        # Normalizace: odstranění interpunkce a mezer, převedení na malá písmena
        normalized_name = re.sub(r'[^\w]', '', item['name'].lower())  # odstraní vše kromě písmen a čísliclimit
        
        # Rozdělení hledaného výrazu na slova (už bez interpunkce)
        search_words = re.findall(r'[a-z0-9]+', what.lower())  # např. ["heart", "eyes"]
        
        # Kontrola, zda všechna hledaná slova jsou v názvu (bez ohledu na pořadí)
        if all(word in normalized_name for word in search_words):
            commands = [(
                _addon.getLocalizedString(30214),
                f'Container.Update({get_url(action="search", toqueue=item["ident"], what=what, offset=offset)})'
            )]
            listitem = tolistitem(item, commands)
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='play', ident=item['ident'], name=item['name']),
                listitem,
                False
            )
    

def normalize_season_episode(search_str):
    """Normalizuje formát řady a dílu (S1E2 -> S01E02, S10E10 zůstane S10E10)"""
    pattern = re.compile(r'(?i)S(\d{1,2})E(\d{1,2})')
    
    def replacer(match):
        season = match.group(1).zfill(2)  # Doplnění nulou pro 1-9
        episode = match.group(2).zfill(2)  # Doplnění nulou pro 1-9
        return f"S{season}E{episode}"
    
    return pattern.sub(replacer, search_str)

def search(params):
    xbmcplugin.setPluginCategory(_handle, f"{_addon.getAddonInfo('name')} / {_addon.getLocalizedString(30201)}")
    token = revalidate()
    updateListing = False
    
    if 'remove' in params:
        removesearch(params['remove'])
        updateListing = True
        
    if 'toqueue' in params:
        toqueue(params['toqueue'], token)
        updateListing = True
    
    what = params.get('what')
    
    # Automaticky zobrazit dialog pro hledání pokud není zadán parametr what
    if what is None and 'ask' not in params:
        what = ask('')
        if what is not None:
            storesearch(what)
        else:
            updateListing = True
    
    if what is not None:
        if 'offset' not in params:
            _addon.setSetting('slast', what)
        else:
            _addon.setSetting('slast', NONE_WHAT)
            updateListing = True
        
        category = params.get('category', CATEGORIES[int(_addon.getSetting('scategory'))])
        sort = params.get('sort', SORTS[int(_addon.getSetting('ssort'))])
        limit = int(params.get('limit', 500))
        offset = int(params.get('offset', 0))
        dosearch(token, what, category, sort, limit, offset, 'search')
    else:
        _addon.setSetting('slast', NONE_WHAT)
        history = loadsearch()
        
        # Search box
        listitem = xbmcgui.ListItem(label=_addon.getLocalizedString(30205))
        listitem.setArt({'icon': 'DefaultAddSource.png'})
        xbmcplugin.addDirectoryItem(_handle, get_url(action='search', what=''), listitem, True)
        
        # Newest
        listitem = xbmcgui.ListItem(label=_addon.getLocalizedString(30208))
        listitem.setArt({'icon': 'DefaultAddonsRecentlyUpdated.png'})
        xbmcplugin.addDirectoryItem(_handle, get_url(action='search', what=NONE_WHAT, sort=SORTS[1]), listitem, True)
        
        # Biggest
        listitem = xbmcgui.ListItem(label=_addon.getLocalizedString(30209))
        listitem.setArt({'icon': 'DefaultHardDisk.png'})
        xbmcplugin.addDirectoryItem(_handle, get_url(action='search', what=NONE_WHAT, sort=SORTS[3]), listitem, True)
        
        # Search history
        for search_term in history:
            listitem = xbmcgui.ListItem(label=search_term)
            listitem.setArt({'icon': 'DefaultAddonsSearch.png'})
            commands = [(
                _addon.getLocalizedString(30213),
                f'Container.Update({get_url(action="search", remove=search_term)})'
            )]
            listitem.addContextMenuItems(commands)
            xbmcplugin.addDirectoryItem(_handle, get_url(action='search', what=search_term, ask=1), listitem, True)
    
    xbmcplugin.endOfDirectory(_handle, updateListing=updateListing)

def queue(params):
    xbmcplugin.setPluginCategory(_handle, f"{_addon.getLocalizedString(30202)}")
    token = revalidate()
    updateListing = False
    
    if 'dequeue' in params:
        response = api('dequeue_file', {'ident': params['dequeue'], 'wst': token})
        xml = ET.fromstring(response.content)
        if is_ok(xml):
            popinfo(_addon.getLocalizedString(30106))
        else:
            popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)
        updateListing = True
    
    response = api('queue', {'wst': token})
    xml = ET.fromstring(response.content)
    
    if is_ok(xml):
        for file in xml.iter('file'):
            item = todict(file)
            commands = [(
                _addon.getLocalizedString(30215),
                f'Container.Update({get_url(action="queue", dequeue=item["ident"])})'
            )]
            listitem = tolistitem(item, commands)
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='play', ident=item['ident'], name=item['name']),
                listitem,
                False
            )
    else:
        popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)
    
    xbmcplugin.endOfDirectory(_handle, updateListing=updateListing)

def toqueue(ident, token):
    response = api('queue_file', {'ident': ident, 'wst': token})
    xml = ET.fromstring(response.content)
    if is_ok(xml):
        popinfo(_addon.getLocalizedString(30105))
    else:
        popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)

def history(params):
    xbmcplugin.setPluginCategory(_handle, f"{_addon.getLocalizedString(30203)}")
    token = revalidate()
    updateListing = False
    
    if 'remove' in params:
        remove = params['remove']
        updateListing = True
        response = api('history', {'wst': token})
        xml = ET.fromstring(response.content)
        ids = []
        
        if is_ok(xml):
            for file in xml.iter('file'):
                if remove == file.find('ident').text:
                    ids.append(file.find('download_id').text)
        else:
            popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)
        
        if ids:
            rr = api('clear_history', {'ids[]': ids, 'wst': token})
            xml = ET.fromstring(rr.content)
            if is_ok(xml):
                popinfo(_addon.getLocalizedString(30104))
            else:
                popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)
    
    if 'toqueue' in params:
        toqueue(params['toqueue'], token)
        updateListing = True
    
    response = api('history', {'wst': token})
    xml = ET.fromstring(response.content)
    files = []
    
    if is_ok(xml):
        for file in xml.iter('file'):
            item = todict(file, ['ended_at', 'download_id', 'started_at'])
            if item not in files:
                files.append(item)
        
        for file in files:
            commands = [
                (_addon.getLocalizedString(30213), f'Container.Update({get_url(action="history", remove=file["ident"])})'),
                (_addon.getLocalizedString(30214), f'Container.Update({get_url(action="history", toqueue=file["ident"])})')
            ]
            listitem = tolistitem(file, commands)
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='play', ident=file['ident'], name=file['name']),
                listitem,
                False
            )
    else:
        popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)
    
    xbmcplugin.endOfDirectory(_handle, updateListing=updateListing)

def settings(params):
    _addon.openSettings()
    xbmcplugin.setResolvedUrl(_handle, False, xbmcgui.ListItem())

def infonize(data, key, process=str, showkey=True, prefix='', suffix='\n'):
    if key in data:
        value = process(data[key]) if callable(process) else str(data[key])
        return f"{prefix}{key.capitalize() + ': ' if showkey else ''}{value}{suffix}"
    return ''

def fpsize(fps):
    try:
        x = round(float(fps), 3)
        return str(int(x)) if int(x) == x else str(x)
    except (ValueError, TypeError):
        return str(fps)

def getinfo(ident, wst):
    try:
        response = api('file_info', {'ident': ident, 'wst': wst})
        xml = ET.fromstring(response.content)
        
        if not is_ok(xml):
            response = api('file_info', {'ident': ident, 'wst': wst, 'maybe_removed': 'true'})
            xml = ET.fromstring(response.content)
        
        if is_ok(xml):
            return xml
    except Exception as e:
        log(f"Error getting file info: {str(e)}", xbmc.LOGERROR)
    
    popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)
    return None

def info(params):
    token = revalidate()
    xml = getinfo(params['ident'], token)
    
    if xml is None:
        return
    
    info = todict(xml)
    text = ''
    text += infonize(info, 'name')
    text += infonize(info, 'size', sizelize)
    text += infonize(info, 'type')
    text += infonize(info, 'width')
    text += infonize(info, 'height')
    text += infonize(info, 'format')
    text += infonize(info, 'fps', fpsize)
    text += infonize(info, 'bitrate', lambda x: sizelize(x, ['bps', 'Kbps', 'Mbps', 'Gbps']))
    
    if 'video' in info and 'stream' in info['video']:
        streams = info['video']['stream']
        streams = [streams] if isinstance(streams, dict) else streams
        for stream in streams:
            text += 'Video stream: '
            text += infonize(stream, 'width', showkey=False, suffix='')
            text += infonize(stream, 'height', showkey=False, prefix='x', suffix='')
            text += infonize(stream, 'format', showkey=False, prefix=', ', suffix='')
            text += infonize(stream, 'fps', fpsize, showkey=False, prefix=', ', suffix='')
            text += '\n'
    
    if 'audio' in info and 'stream' in info['audio']:
        streams = info['audio']['stream']
        streams = [streams] if isinstance(streams, dict) else streams
        for stream in streams:
            text += 'Audio stream: '
            text += infonize(stream, 'format', showkey=False, suffix='')
            text += infonize(stream, 'channels', prefix=', ', showkey=False, suffix='')
            text += infonize(stream, 'bitrate', lambda x: sizelize(x, ['bps', 'Kbps', 'Mbps', 'Gbps']), prefix=', ', showkey=False, suffix='')
            text += '\n'
    
    text += infonize(info, 'removed', lambda x: 'Yes' if x == '1' else 'No')
    xbmcgui.Dialog().textviewer(_addon.getAddonInfo('name'), text)

def getlink(ident, wst, dtype='video_stream'):
    duuid = _addon.getSetting('duuid')
    if not duuid:
        duuid = str(uuid.uuid4())
        _addon.setSetting('duuid', duuid)
    
    data = {
        'ident': ident,
        'wst': wst,
        'download_type': dtype,
        'device_uuid': duuid
    }
    
    response = api('file_link', data)
    xml = ET.fromstring(response.content)
    
    if is_ok(xml):
        return xml.find('link').text
    
    popinfo(_addon.getLocalizedString(30107), icon=xbmcgui.NOTIFICATION_WARNING)
    return None

def play(params):
    token = revalidate()
    link = getlink(params['ident'], token)
    
    if link is None:
        xbmcplugin.setResolvedUrl(_handle, False, xbmcgui.ListItem())
        return
    
    headers = _session.headers.copy()
    headers.update({'Cookie': f'wst={token}'})
    link = f"{link}|{urlencode(headers)}"
    
    listitem = xbmcgui.ListItem(label=params['name'], path=link)
    listitem.setProperty('mimetype', 'application/octet-stream')
    xbmcplugin.setResolvedUrl(_handle, True, listitem)

def join(path, file):
    return os.path.join(path, file) if not path.endswith(('/', '//')) else path + file

def download(params):
    token = revalidate()
    where = _addon.getSetting('dfolder')
    
    if not where or not xbmcvfs.exists(where):
        popinfo('Set download folder!', sound=True)
        _addon.openSettings()
        return
    
    local = os.path.exists(where)
    normalize = _addon.getSetting('dnormalize') == 'true'
    notify = _addon.getSetting('dnotify') == 'true'
    
    try:
        every = int(re.sub(r'[^\d]+', '', _addon.getSetting('dnevery') or '10'))
    except ValueError:
        every = 10
    
    try:
        link = getlink(params['ident'], token, 'file_download')
        if not link:
            return
            
        info = getinfo(params['ident'], token)
        if not info:
            return
            
        name = info.find('name').text
        if normalize:
            name = unidecode.unidecode(name)
        
        filepath = join(where, name)
        
        if local:
            bf = io.open(filepath, 'wb')
        else:
            bf = xbmcvfs.File(filepath, 'w')
        
        response = _session.get(link, stream=True, timeout=60)
        total = response.headers.get('content-length')
        
        if total is None:
            popinfo(f"{_addon.getLocalizedString(30301)} {name}", icon=xbmcgui.NOTIFICATION_WARNING, sound=True)
            bf.write(response.content)
        elif not notify:
            popinfo(f"{_addon.getLocalizedString(30302)} {name}")
            bf.write(response.content)
        else:
            popinfo(f"{_addon.getLocalizedString(30302)} {name}")
            dl = 0
            total = int(total)
            pct = total / 100
            lastpop = 0
            
            for data in response.iter_content(chunk_size=4096):
                dl += len(data)
                bf.write(data)
                done = int(dl / pct)
                
                if done % every == 0 and lastpop != done:
                    popinfo(f"{done}% - {name}")
                    lastpop = done
        
        bf.close()
        popinfo(f"{_addon.getLocalizedString(30303)} {name}", sound=True)
    except Exception as e:
        log(f"Download failed: {str(e)}", xbmc.LOGERROR)
        popinfo(f"{_addon.getLocalizedString(30304)} {name}", icon=xbmcgui.NOTIFICATION_ERROR, sound=True)

def loaddb(dbdir, filename):
    try:
        with io.open(os.path.join(dbdir, filename), 'r', encoding='utf-8') as file:
            return json.load(file)['data']
    except Exception as e:
        log(f"Error loading DB {filename}: {str(e)}", xbmc.LOGERROR)
        return {}

def db(params):
    token = revalidate()
    updateListing = False
    dbdir = os.path.join(_profile, 'db')
    search_query = params.get('search', '').lower()
    
    # Download DB if needed
    if not os.path.exists(dbdir):
        try:
            os.makedirs(dbdir, exist_ok=True)
            link = getlink(BACKUP_DB, token)
            if not link:
                return
                
            dbfile = os.path.join(_profile, 'db.zip')
            
            with io.open(dbfile, 'wb') as bf:
                response = _session.get(link, stream=True)
                bf.write(response.content)
            
            with zipfile.ZipFile(dbfile, 'r') as zf:
                zf.extractall(_profile)
            
            os.unlink(dbfile)
        except Exception as e:
            log(f"Error downloading DB: {str(e)}", xbmc.LOGERROR)
            return
    
    if 'toqueue' in params:
        toqueue(params['toqueue'], token)
        updateListing = True
    
    # Streamy
    if 'file' in params and 'key' in params:
        data = loaddb(dbdir, params['file'])
        item = next((x for x in data if x['id'] == params['key']), None)
        
        if item is not None:
            for stream in item['streams']:
                commands = [(
                    _addon.getLocalizedString(30214),
                    f'Container.Update({get_url(action="db", file=params["file"], key=params["key"], toqueue=stream["ident"])})'
                )]
                listitem = tolistitem({
                    'ident': stream['ident'],
                    'name': f"{stream['quality']} - {stream['lang']}{stream.get('ainfo', '')}",
                    'sizelized': stream['size']
                }, commands)
                xbmcplugin.addDirectoryItem(
                    _handle,
                    get_url(action='play', ident=stream['ident'], name=item['title']),
                    listitem,
                    False
                )
        xbmcplugin.addSortMethod(_handle, xbmcplugin.SORT_METHOD_LABEL)

    # Film/seriál
    elif 'file' in params:
        data = loaddb(dbdir, params['file'])
        for item in data:
            # Skip items that don't match search query if one is provided
            if search_query and search_query not in item['title'].lower():
                continue
                
            listitem = xbmcgui.ListItem(label=item['title'])
            if 'plot' in item:
                listitem.setInfo('video', {'title': item['title'], 'plot': item['plot']})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='db', file=params['file'], key=item['id']),
                listitem,
                True
            )

        xbmcplugin.addSortMethod(_handle, xbmcplugin.SORT_METHOD_LABEL)

    # Složky A-Z
    else:
        if os.path.exists(dbdir):
            # Add search item if not already in search mode

            if not search_query:
                search_item = xbmcgui.ListItem(label=_addon.getLocalizedString(30010)) 
                search_item.setArt({'icon': 'DefaultAddonsSearch.png'})  # Set icon
                xbmcplugin.addDirectoryItem(
                    _handle,
                    get_url(action='searchdb'),
                    search_item,
                    True
                )

            
            for dbfile in [f for f in os.listdir(dbdir) if os.path.isfile(os.path.join(dbdir, f))]:
                listitem = xbmcgui.ListItem(label=os.path.splitext(dbfile)[0])
                xbmcplugin.addDirectoryItem(
                    _handle,
                    get_url(action='db', file=dbfile),
                    listitem,
                    True
                )
    
    # xbmcplugin.addSortMethod(_handle, xbmcplugin.SORT_METHOD_LABEL)
    xbmcplugin.endOfDirectory(_handle, updateListing=updateListing)


def searchdb(params=None):
    search_query = ''
    if params and 'query' in params:
        search_query = params['query']
    else:
        keyboard = xbmc.Keyboard('', _addon.getLocalizedString(30216))  # "Search for"
        keyboard.doModal()
        if keyboard.isConfirmed():
            search_query = keyboard.getText()
    
    if search_query:
        # Get first character of search query (uppercase)
        first_char = search_query[0].upper()
        # For non-ASCII characters or numbers, use '0'
        if not first_char.isalpha():
            first_char = '0'
        # Redirect to db function with search parameter and appropriate file
        db({'search': search_query, 'file': f'{first_char}.txt'})

def menu():
    revalidate()
    xbmcplugin.setPluginCategory(_handle, _addon.getAddonInfo('name'))
    
    # Search
    listitem = xbmcgui.ListItem(label=_addon.getLocalizedString(30201))
    listitem.setArt({'icon': 'DefaultAddonsSearch.png'})
    xbmcplugin.addDirectoryItem(_handle, get_url(action='search'), listitem, True)
    
    # Queue
    listitem = xbmcgui.ListItem(label=_addon.getLocalizedString(30202))
    listitem.setArt({'icon': 'DefaultPlaylist.png'})
    xbmcplugin.addDirectoryItem(_handle, get_url(action='queue'), listitem, True)
    
    # History
    listitem = xbmcgui.ListItem(label=_addon.getLocalizedString(30203))
    listitem.setArt({'icon': 'DefaultAddonsUpdates.png'})
    xbmcplugin.addDirectoryItem(_handle, get_url(action='history'), listitem, True)
    
    # DB
    listitem = xbmcgui.ListItem(label='Backup DB')
    listitem.setArt({'icon': 'DefaultAddonsZip.png'})
    xbmcplugin.addDirectoryItem(_handle, get_url(action='db'), listitem, True)

    # TMDB Watchlist
    listitem = xbmcgui.ListItem(label='TMDB Watchlist')
    listitem.setArt({'icon': 'DefaultVideoPlaylists.png'})
    xbmcplugin.addDirectoryItem(_handle, get_url(action='tmdb_watchlist'), listitem, True)

    # Settings
    listitem = xbmcgui.ListItem(label=_addon.getLocalizedString(30204))
    listitem.setArt({'icon': 'DefaultAddonService.png'})
    xbmcplugin.addDirectoryItem(_handle, get_url(action='settings'), listitem, False)

    xbmcplugin.endOfDirectory(_handle)

def router(paramstring):
    params = dict(parse_qsl(paramstring))
    
    if not params:
        menu()
        return
    
    action = params.get('action')
    
    if action == 'search':
        search(params)
    elif action == 'queue':
        queue(params)
    elif action == 'history':
        history(params)
    elif action == 'settings':
        settings(params)
    elif action == 'info':
        info(params)
    elif action == 'play':
        play(params)
    elif action == 'download':
        download(params)
    elif action == 'db':
        db(params)
    elif action == 'tmdb_watchlist':
        tmdb_watchlist(params)
    elif action == 'tmdb_auth':
        tmdb_authenticate()
        xbmcplugin.setResolvedUrl(_handle, False, xbmcgui.ListItem())
    elif action == 'searchdb':
        searchdb(params)
    else:
        menu()

def tmdb_get_api_key():
    """API klíč z nastavení doplňku"""
    return _addon.getSetting('tmdb_api_key').strip()

def tmdb_get_session():
    return _addon.getSetting('tmdb_access_token').strip()

def tmdb_request(method, path, params=None, json_data=None, query_params=None):
    """Společný request na TMDB s retry při rate limitu (429).

    Autentizace: api_key (+ session_id, pokud je uživatel připojen)."""
    query = {'language': 'cs-CZ'}
    if params:
        query.update(params)

    headers = {'accept': 'application/json', 'Content-Type': 'application/json'}
    api_key = tmdb_get_api_key()
    if api_key:
        query['api_key'] = api_key
    session_id = tmdb_get_session()
    if session_id:
        query['session_id'] = session_id
    if query_params:
        query.update(query_params)

    for attempt in range(3):
        response = _session.request(
            method,
            TMDB_API_URL + path,
            headers=headers,
            params=query,
            json=json_data,
            timeout=15
        )
        if response.status_code == 429 and attempt < 2:
            retry_after = min(int(response.headers.get('Retry-After', 1)), 5)
            log(f"TMDB rate limit, retry za {retry_after}s")
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        return response
    return response

def tmdb_get(path, params=None):
    return tmdb_request('GET', path, params=params).json()

def tmdb_set_watchlist(media_type, media_id, watchlist):
    """Zápis do watchlistu přes v3 session (429-safe)"""
    response = tmdb_request(
        'POST',
        f'/3/account/{tmdb_get_account_id()}/watchlist',
        json_data={'media_type': media_type, 'media_id': media_id, 'watchlist': watchlist}
    )
    return response.status_code in (200, 201) and response.json().get('success')

def tmdb_get_account_id():
    account_id = _addon.getSetting('tmdb_account_id').strip()
    if account_id:
        return account_id
    data = tmdb_get('/3/account')
    account_id = str(data.get('id', ''))
    _addon.setSetting('tmdb_account_id', account_id)
    log(f"TMDB account_id: {account_id}")
    return account_id

def set_view_infowall(params):
    """Po načtení watchlistu vynutí pohled InfoWall (Estuary view id 54)"""
    if params.get('category') in ('movies', 'shows') and xbmc.getSkinDir() == 'skin.estuary':
        xbmc.executebuiltin('Container.SetViewMode(54)')

def tmdb_genres(media):
    if media not in _TMDB_GENRES_CACHE:
        data = tmdb_get(f'/3/genre/{media}/list')
        _TMDB_GENRES_CACHE[media] = {g['id']: g['name'] for g in data.get('genres', [])}
    return _TMDB_GENRES_CACHE[media]

def tmdb_load_trailers():
    global _TMDB_TRAILERS_LOADED
    if _TMDB_TRAILERS_LOADED:
        return
    _TMDB_TRAILERS_LOADED = True
    try:
        path = os.path.join(_profile, TMDB_TRAILERS_FILE)
        if os.path.exists(path):
            with io.open(path, 'r', encoding='utf-8') as f:
                for key, value in json.load(f).items():
                    media, media_id = key.split(':', 1)
                    _TMDB_TRAILERS_CACHE[(media, int(media_id))] = value or None
    except Exception as e:
        log(f"Error loading trailer cache: {str(e)}", xbmc.LOGERROR)

def tmdb_save_trailers():
    try:
        os.makedirs(_profile, exist_ok=True)
        data = {f"{media}:{media_id}": yt for (media, media_id), yt in _TMDB_TRAILERS_CACHE.items()}
        with io.open(os.path.join(_profile, TMDB_TRAILERS_FILE), 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        log(f"Error saving trailer cache: {str(e)}", xbmc.LOGERROR)

def tmdb_trailer(media, media_id):
    tmdb_load_trailers()
    cache_key = (media, media_id)
    if cache_key not in _TMDB_TRAILERS_CACHE:
        youtube_id = None
        try:
            data = tmdb_get(f'/3/{media}/{media_id}/videos', {'language': 'en-US'})
            videos = [v for v in data.get('results', []) if v.get('site') == 'YouTube']
            trailers = [v for v in videos if v.get('type') == 'Trailer']
            official = [v for v in trailers if v.get('official')]
            best = (official or trailers or videos or [None])[0]
            if best:
                youtube_id = best.get('key')
        except Exception as e:
            log(f"TMDB trailer error ({media} {media_id}): {str(e)}", xbmc.LOGERROR)
        _TMDB_TRAILERS_CACHE[cache_key] = youtube_id
    return _TMDB_TRAILERS_CACHE[cache_key]

def tmdb_prefetch_trailers(media, media_ids):
    """Paralelně načte trailery pro všechny položky bez cache"""
    tmdb_load_trailers()
    missing = [(media, media_id) for media_id in media_ids if (media, media_id) not in _TMDB_TRAILERS_CACHE]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda m: tmdb_trailer(*m), missing))
    tmdb_save_trailers()

def tmdb_load_shows():
    global _TMDB_SHOWS_LOADED
    if _TMDB_SHOWS_LOADED:
        return
    _TMDB_SHOWS_LOADED = True
    try:
        path = os.path.join(_profile, TMDB_SHOWS_FILE)
        if os.path.exists(path):
            with io.open(path, 'r', encoding='utf-8') as f:
                _TMDB_SHOWS_CACHE.update({int(k): v for k, v in json.load(f).items()})
    except Exception as e:
        log(f"Error loading shows cache: {str(e)}", xbmc.LOGERROR)

def tmdb_save_shows():
    try:
        os.makedirs(_profile, exist_ok=True)
        with io.open(os.path.join(_profile, TMDB_SHOWS_FILE), 'w', encoding='utf-8') as f:
            json.dump(_TMDB_SHOWS_CACHE, f)
    except Exception as e:
        log(f"Error saving shows cache: {str(e)}", xbmc.LOGERROR)

def tmdb_show_next(show_id):
    """Vrátí info o další epizodě {'season','episode','air_date'} nebo None (cache 24h)"""
    entry = _TMDB_SHOWS_CACHE.get(show_id)
    if not entry or time.time() - entry.get('ts', 0) > TMDB_SHOWS_TTL:
        nxt = None
        try:
            data = tmdb_get(f'/3/tv/{show_id}')
            ne = data.get('next_episode_to_air')
            if ne:
                nxt = {
                    'season': ne.get('season_number'),
                    'episode': ne.get('episode_number'),
                    'air_date': ne.get('air_date') or ''
                }
        except Exception as e:
            log(f"TMDB show detail error ({show_id}): {str(e)}", xbmc.LOGERROR)
        entry = {'ts': time.time(), 'next': nxt}
        _TMDB_SHOWS_CACHE[show_id] = entry
    return entry.get('next')

def tmdb_prefetch_shows(show_ids):
    """Paralelně načte detaily seriálů (další díl) bez platné cache"""
    tmdb_load_shows()
    now = time.time()
    missing = [sid for sid in show_ids
               if sid not in _TMDB_SHOWS_CACHE or now - _TMDB_SHOWS_CACHE[sid].get('ts', 0) > TMDB_SHOWS_TTL]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(tmdb_show_next, missing))
    tmdb_save_shows()

def tmdb_ensure_session():
    """Vrátí True, pokud je k dispozici session (případně po spuštění auth flow)"""
    if tmdb_get_session():
        return True
    return bool(tmdb_authenticate() and tmdb_get_session())

def tmdb_qr_png(url):
    """Vytvoří čtvercové PNG s QR kódem odkazu. Vrátí cestu nebo None."""
    try:
        import qrcodegen
        qr = qrcodegen.QrCode.encode_text(url, qrcodegen.QrCode.Ecc.MEDIUM)

        scale = 8
        border = 4
        size = qr.get_size() + 2 * border
        px = size * scale

        rows = [bytearray(b'\xff' * px * 3) for _ in range(px)]
        for my in range(qr.get_size()):
            for mx in range(qr.get_size()):
                if qr.get_module(mx, my):
                    my0 = (my + border) * scale
                    mx0 = (mx + border) * scale
                    for dy in range(scale):
                        row = rows[my0 + dy]
                        base = mx0 * 3
                        for dx in range(scale):
                            row[base + dx * 3:base + dx * 3 + 3] = b'\x00\x00\x00'

        def chunk(tag, data):
            raw = tag + data
            return struct.pack('>I', len(data)) + raw + struct.pack('>I', zlib.crc32(raw) & 0xffffffff)

        raw = b''.join(b'\x00' + bytes(r) for r in rows)
        png = (b'\x89PNG\r\n\x1a\n'
               + chunk(b'IHDR', struct.pack('>IIBBBBB', px, px, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(raw, 6))
               + chunk(b'IEND', b''))

        os.makedirs(_profile, exist_ok=True)
        path = os.path.join(_profile, 'tmdb_qr.png')
        with io.open(path, 'wb') as f:
            f.write(png)
        return path
    except Exception as e:
        log(f"QR generation failed: {str(e)}", xbmc.LOGERROR)
        return None

def tmdb_create_session(api_key, request_token):
    """Zkusí vytvořit session z request tokenu (vrátí session_id nebo None)"""
    response = _session.post(
        f'{TMDB_API_URL}/3/authentication/session/new',
        params={'api_key': api_key},
        json={'request_token': request_token},
        timeout=15
    )
    if response.status_code == 200:
        return response.json().get('session_id')
    return None

@handle_errors
def tmdb_authenticate():
    """Handle TMDB user authentication (v3 session flow).

    Zobrazí QR kód fullscreen (bez dialogu, který by ho zakryl) a čeká na schválení."""
    WINDOW_PICTURES = 12007

    api_key = tmdb_get_api_key()
    if not api_key:
        popinfo("Nejprve vyplňte API Key v nastavení doplňku.", icon=xbmcgui.NOTIFICATION_ERROR)
        _addon.openSettings()
        return False

    response = _session.get(f'{TMDB_API_URL}/3/authentication/token/new', params={'api_key': api_key}, timeout=15)
    response.raise_for_status()
    request_token = response.json()['request_token']

    url = f'{TMDB_AUTH_URL}{request_token}'
    log(f"TMDB auth odkaz: {url}", xbmc.LOGINFO)

    use_qr = False
    qr_path = tmdb_qr_png(url)
    if qr_path:
        use_qr = True
        xbmc.executebuiltin(f'ShowPicture({qr_path})')
        popinfo("Naskenujte QR kód telefonem a schvalte přístup", time=5000)
    else:
        popinfo(f"Schvalte přístup v prohlížeči: {url}", icon=xbmcgui.NOTIFICATION_WARNING, time=15000)

    # Čekání na schválení (polling), max ~5 minut
    monitor = xbmc.Monitor()
    deadline = time.time() + 300
    viewer_shown = False
    while time.time() < deadline:
        if use_qr:
            wid = xbmcgui.getCurrentWindowId()
            if wid == WINDOW_PICTURES:
                viewer_shown = True
            elif viewer_shown:
                log("TMDB auth: uživatel zavřel QR okno")
                return False
        session_id = tmdb_create_session(api_key, request_token)
        if session_id:
            _addon.setSetting('tmdb_access_token', session_id)
            popinfo("Úspěšně připojeno k TMDB!", sound=True)
            if use_qr:
                xbmc.executebuiltin('Action(Back)')
            return True
        if monitor.waitForAbort(4):
            return False

    popinfo("Čas na ověření vypršel.", icon=xbmcgui.NOTIFICATION_ERROR)
    return False

@handle_errors
def tmdb_watchlist(params):
    xbmcplugin.setPluginCategory(_handle, "TMDB Watchlist")

    if not tmdb_get_api_key():
        popinfo("Pro připojení k TMDB je třeba vyplnit API Key v nastavení.", sound=True)
        _addon.openSettings()
        xbmcplugin.endOfDirectory(_handle)
        return

    has_session = bool(tmdb_get_session())

    if 'category' not in params:
        if has_session:
            # Movies folder
            listitem = xbmcgui.ListItem(label="Filmy")
            listitem.setArt({'icon': 'DefaultMovies.png'})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='tmdb_watchlist', category='movies'),
                listitem,
                True
            )

            # TV Shows folder
            listitem = xbmcgui.ListItem(label="Seriály")
            listitem.setArt({'icon': 'DefaultTVShows.png'})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='tmdb_watchlist', category='shows'),
                listitem,
                True
            )
        else:
            # Navigate to settings (connect happens there)
            listitem = xbmcgui.ListItem(label="Připojit k TMDB v nastavení doplňku...")
            listitem.setArt({'icon': 'DefaultAddonService.png'})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='settings'),
                listitem,
                False
            )

        xbmcplugin.endOfDirectory(_handle)
        return

    if not has_session:
        popinfo("Pro zobrazení watchlistu se připoj k TMDB v nastavení doplňku.", icon=xbmcgui.NOTIFICATION_WARNING)
        _addon.openSettings()
        xbmcplugin.endOfDirectory(_handle)
        return

    try:
        # Označení jako zhlédnuté = zároveň odstranění z watchlistu (musí být PRVNÍ před načtením seznamu)
        if 'watched' in params:
            if not tmdb_ensure_session():
                popinfo("Pro tuto akci je třeba se nejprve připojit k TMDB.", icon=xbmcgui.NOTIFICATION_ERROR)
                xbmc.executebuiltin('Container.Refresh()')
                return

            media_type = 'movie' if params['category'] == 'movies' else 'tv'
            if tmdb_set_watchlist(media_type, int(params['watched']), False):
                popinfo("Zhlédnuto a odstraněno z watchlistu")
            else:
                popinfo("Chyba při odstraňování z watchlistu", icon=xbmcgui.NOTIFICATION_ERROR)

            xbmc.executebuiltin('Container.Refresh()')
            return

        # Handle seasons listing for a show
        if 'show_id' in params and 'season' not in params:
            return list_seasons(params)

        # Handle episodes listing for a season
        if 'show_id' in params and 'season' in params:
            return list_episodes(params)

        # Načtení watchlistu (všechny stránky, česky)
        media = 'movie' if params['category'] == 'movies' else 'tv'
        watchlist_media = 'movies' if params['category'] == 'movies' else 'tv'
        is_movie = params['category'] == 'movies'
        sort_mode = int(_addon.getSetting('tmdb_sort') or 0)

        page_params = {'sort_by': 'created_at.desc'} if sort_mode == 1 else {}
        results = []
        page = 1
        while True:
            data = tmdb_get(f'/3/account/{tmdb_get_account_id()}/watchlist/{watchlist_media}', dict(page_params, page=page))
            results.extend(data.get('results', []))
            if page >= data.get('total_pages', 1):
                break
            page += 1

        # Filtr zatím nevydaných filmů
        if is_movie and _addon.getSetting('tmdb_hide_unreleased') == 'true':
            today_str = str(datetime.now().date())
            results = [it for it in results if not it.get('release_date') or it['release_date'] <= today_str]

        # Řazení (0 = abecedně, 1 = pořadí z API = naposledy přidané)
        if sort_mode == 0:
            results = sorted(results, key=lambda x: (x.get('title') or x.get('name') or '').lower())
        elif sort_mode == 2:
            results = sorted(results, key=lambda x: float(x.get('vote_average') or 0), reverse=True)
        elif sort_mode == 3:
            results = sorted(results, key=lambda x: x.get('release_date') or x.get('first_air_date') or '', reverse=True)

        genres = tmdb_genres(media)
        tmdb_prefetch_trailers(media, [item['id'] for item in results])
        if not is_movie:
            tmdb_prefetch_shows([item['id'] for item in results])

        for item in results:
            media_id = item['id']
            title = item.get('title') if is_movie else item.get('name')
            original_title = item.get('original_title') if is_movie else item.get('original_name')
            title = title or original_title or 'Neznámý název'
            plot = item.get('overview', '')
            year = (item.get('release_date') or item.get('first_air_date') or '')[:4]
            label = f"{title} ({year})" if year else title

            # Info o další epizodě u seriálů
            if not is_movie:
                nxt = tmdb_show_next(media_id)
                if nxt:
                    next_str = f"S{int(nxt.get('season') or 0):02d}E{int(nxt.get('episode') or 0):02d}"
                    if nxt.get('air_date'):
                        try:
                            ny, nm, nd = map(int, nxt['air_date'].split('-'))
                            next_str += f" ({nd}. {nm}. {ny})"
                        except Exception:
                            pass
                    label += f" [COLOR gray]→ {next_str}[/COLOR]"

            artwork = {}
            if item.get('poster_path'):
                artwork['poster'] = f"{TMDB_IMAGE_URL}w500{item['poster_path']}"
                artwork['thumb'] = artwork['poster']
            if item.get('backdrop_path'):
                artwork['fanart'] = f"{TMDB_IMAGE_URL}w780{item['backdrop_path']}"

            listitem = xbmcgui.ListItem(label=label)
            if artwork:
                listitem.setArt(artwork)

            context_menu_items = [(
                'Vyhledat původní název',
                f'Container.Update({get_url(action="search", what=original_title or title)})'
            )]

            youtube_id = tmdb_trailer(media, media_id)
            if youtube_id:
                context_menu_items.append((
                    "Přehrát trailer",
                    f'PlayMedia(plugin://plugin.video.youtube/play/?video_id={youtube_id})'
                ))

            if _addon.getSetting('tmdb_access_token').strip():
                context_menu_items.append((
                    'Označit jako zhlédnuté',
                    f'RunPlugin({get_url(action="tmdb_watchlist", category=params["category"], watched=media_id)})'
                ))

            listitem.addContextMenuItems(context_menu_items)

            info = {
                'title': label,
                'mediatype': 'movie' if is_movie else 'tvshow',
                'plot': plot,
                'year': int(year) if year else 0,
                'genre': ' / '.join(genres[g] for g in item.get('genre_ids', []) if g in genres),
                'rating': float(item.get('vote_average') or 0),
            }
            listitem.setInfo('video', info)

            if is_movie:
                url = get_url(action='search', what=title)
            else:
                url = get_url(action='tmdb_watchlist', show_id=media_id, category='shows')

            xbmcplugin.addDirectoryItem(_handle, url, listitem, True)

        if not results:
            listitem = xbmcgui.ListItem(label="Watchlist je prázdný")
            listitem.setArt({'icon': 'DefaultVideoPlaylists.png'})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='tmdb_watchlist', category=params['category']),
                listitem,
                False
            )

    except Exception as e:
        log(f"TMDB chyba: {str(e)}", xbmc.LOGERROR)
        if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 401:
            popinfo("Neplatný TMDB API klíč nebo vypršené přihlášení – zkontroluj nastavení.", icon=xbmcgui.NOTIFICATION_ERROR, sound=True)
            _addon.openSettings()
        else:
            popinfo("Chyba při načítání", icon=xbmcgui.NOTIFICATION_ERROR)
        traceback.print_exc()

    xbmcplugin.setContent(_handle, 'movies' if params.get('category') == 'movies' else 'tvshows')
    xbmcplugin.endOfDirectory(_handle)
    set_view_infowall(params)

@handle_errors
def list_seasons(params):
    """List all seasons for a show"""
    show_id = params['show_id']

    show = tmdb_get(f'/3/tv/{show_id}')
    title = show.get('name') or show.get('original_name') or 'Neznámý název'
    original_title = show.get('original_name') or title

    xbmcplugin.setPluginCategory(_handle, title)

    # Add each season
    for season in sorted(show.get('seasons', []), key=lambda x: x.get('season_number', 0)):
        season_num = season.get('season_number', 0)
        episode_count = season.get('episode_count', 0)

        listitem = xbmcgui.ListItem(label=f"Sezóna {season_num} ({episode_count} epizod)")
        if season.get('poster_path'):
            listitem.setArt({'poster': f"{TMDB_IMAGE_URL}w500{season['poster_path']}", 'icon': 'DefaultSeason.png'})
        else:
            listitem.setArt({'icon': 'DefaultSeason.png'})

        listitem.setInfo('video', {
            'title': f"Sezóna {season_num}",
            'mediatype': 'season',
            'season': season_num,
            'episode': episode_count,
        })

        xbmcplugin.addDirectoryItem(
            _handle,
            get_url(action='tmdb_watchlist', show_id=show_id, season=season_num, series_title=original_title, category='shows'),
            listitem,
            True
        )

    xbmcplugin.setContent(_handle, 'seasons')
    xbmcplugin.endOfDirectory(_handle)

@handle_errors
def list_episodes(params):
    """List all episodes for a season with detailed info"""
    show_id = params['show_id']
    season_num = params['season']
    series_title = params['series_title']

    xbmcplugin.setPluginCategory(_handle, f"{_addon.getAddonInfo('name')} / Sezóna {season_num}")

    data = tmdb_get(f'/3/tv/{show_id}/season/{season_num}')
    episodes = data.get('episodes', [])
    today = datetime.now().date()

    for episode in episodes:
        ep_num = episode.get('episode_number')
        ep_title = episode.get('name', 'Neznámý název')
        ep_air_date = episode.get('air_date')
        ep_plot = episode.get('overview', '')
        ep_rating = episode.get('vote_average') or 0
        ep_runtime = episode.get('runtime') or 0

        # Formátování data (pokud epizoda ještě nebyla vysílána)
        air_date_str = ""
        is_future = False
        if ep_air_date:
            try:
                year, month, day = map(int, ep_air_date.split('-'))
                air_date_obj = date(year, month, day)

                if air_date_obj > today:
                    is_future = True
                air_date_str = f" ({day:02d}.{month:02d}.{year})"
            except Exception as e:
                log(f"Chyba při zpracování data {ep_air_date}: {str(e)}", xbmc.LOGERROR)

        # Vytvoření popisku
        label = f"{ep_title}"
        if is_future:
            label += f" [COLOR red]{air_date_str}[/COLOR]"
        elif air_date_str:
            label += f" [COLOR gray]{air_date_str}[/COLOR]"

        # Vytvoření položky v seznamu
        listitem = xbmcgui.ListItem(label=label)

        if episode.get('still_path'):
            listitem.setArt({'thumb': f"{TMDB_IMAGE_URL}w300{episode['still_path']}"})

        info = {
            'title': label,
            'mediatype': 'episode',
            'plot': ep_plot,
            'season': int(season_num),
            'episode': int(ep_num),
            'duration': ep_runtime * 60,
            'rating': float(ep_rating),
        }

        if ep_air_date:
            info['aired'] = ep_air_date

        listitem.setInfo('video', info)

        # Přidání do seznamu
        xbmcplugin.addDirectoryItem(
            _handle,
            get_url(action='search', what=f"{series_title} S{int(season_num):02d}E{int(ep_num):02d}"),
            listitem,
            True
        )

    xbmcplugin.addSortMethod(_handle, xbmcplugin.SORT_METHOD_EPISODE)
    xbmcplugin.endOfDirectory(_handle)


if __name__ == '__main__':
    router(sys.argv[2][1:])
