import io
import os
import sys
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs
import requests.cookies
from xml.etree import ElementTree as ET
import hashlib
from md5crypt import md5crypt
import traceback
import json
import unidecode
import time
import re
import zipfile
import uuid
import json
from functools import wraps
from datetime import datetime

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

# Trakt OAuth constants
TRAKT_OAUTH_URL = 'https://api.trakt.tv/oauth/'
TRAKT_AUTHORIZE_URL = TRAKT_OAUTH_URL + 'authorize'
TRAKT_TOKEN_URL = TRAKT_OAUTH_URL + 'token'
TRAKT_DEVICE_CODE_URL = TRAKT_OAUTH_URL + 'device/code'
TRAKT_DEVICE_TOKEN_URL = TRAKT_OAUTH_URL + 'device/token'

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
        xbmcplugin.addDirectoryItem(_handle, get_url(action='search', ask=1), listitem, True)
        
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
    xbmcplugin.setPluginCategory(_handle, f"{_addon.getAddonInfo('name')} \\ {_addon.getLocalizedString(30202)}")
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
    xbmcplugin.setPluginCategory(_handle, f"{_addon.getAddonInfo('name')} \\ {_addon.getLocalizedString(30203)}")
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
    return os.path.join(path, file) if not path.endswith(('/', '\\')) else path + file

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
    elif 'file' in params:
        data = loaddb(dbdir, params['file'])
        for item in data:
            listitem = xbmcgui.ListItem(label=item['title'])
            if 'plot' in item:
                listitem.setInfo('video', {'title': item['title'], 'plot': item['plot']})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='db', file=params['file'], key=item['id']),
                listitem,
                True
            )
    else:
        if os.path.exists(dbdir):
            for dbfile in [f for f in os.listdir(dbdir) if os.path.isfile(os.path.join(dbdir, f))]:
                listitem = xbmcgui.ListItem(label=os.path.splitext(dbfile)[0])
                xbmcplugin.addDirectoryItem(
                    _handle,
                    get_url(action='db', file=dbfile),
                    listitem,
                    True
                )
    
    xbmcplugin.addSortMethod(_handle, xbmcplugin.SORT_METHOD_LABEL)
    xbmcplugin.endOfDirectory(_handle, updateListing=updateListing)

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
    
    # Backup DB (experimental)
    if _addon.getSetting('experimental') == 'true':
        listitem = xbmcgui.ListItem(label='Backup DB')
        listitem.setArt({'icon': 'DefaultAddonsZip.png'})
        xbmcplugin.addDirectoryItem(_handle, get_url(action='db'), listitem, True)

    # Trakt Watchlist
    listitem = xbmcgui.ListItem(label='Trakt Watchlist')
    listitem.setArt({'icon': 'DefaultVideoPlaylists.png'})
    xbmcplugin.addDirectoryItem(_handle, get_url(action='trakt_watchlist'), listitem, True)

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
    elif action == 'trakt_watchlist':
        trakt_watchlist(params)
    else:
        menu()

def trakt_watchlist(params):
    xbmcplugin.setPluginCategory(_handle, f"{_addon.getAddonInfo('name')} \\ Trakt Watchlist")
    
    # Check authentication first
    if 'reauth' in params:
        if trakt_authenticate():
            xbmc.executebuiltin('Container.Refresh()')
        return
    
    trakt_client_id = _addon.getSetting('trakt_client_id').strip()
    if not trakt_client_id:
        popinfo("Pro připojení k Traktu je třeba vyplnit Client ID v nastavení.", sound=True)
        _addon.openSettings()
        xbmcplugin.endOfDirectory(_handle)
        return
    
    try:
        if 'category' not in params:
            # Movies folder
            listitem = xbmcgui.ListItem(label="Filmy")
            listitem.setArt({'icon': 'DefaultMovies.png'})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='trakt_watchlist', category='movies'),
                listitem,
                True
            )
            
            # TV Shows folder
            listitem = xbmcgui.ListItem(label="Seriály")
            listitem.setArt({'icon': 'DefaultTVShows.png'})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='trakt_watchlist', category='shows'),
                listitem,
                True
            )
            
            # Authentication item
            if not _addon.getSetting('trakt_access_token'):
                listitem = xbmcgui.ListItem(label="Připojit k Traktu...")
                listitem.setArt({'icon': 'DefaultAddonService.png'})
                xbmcplugin.addDirectoryItem(
                    _handle,
                    get_url(action='trakt_watchlist', reauth=1),
                    listitem,
                    False
                )
            
            xbmcplugin.endOfDirectory(_handle)
            return
        
        # Check if we're removing an item
        if 'remove' in params:
            access_token = _addon.getSetting('trakt_access_token').strip()
            if not access_token:
                popinfo("Pro tuto akci je třeba se nejprve připojit k Traktu.", icon=xbmcgui.NOTIFICATION_ERROR)
                xbmcplugin.endOfDirectory(_handle)
                return
                
            media_type = 'movie' if params['category'] == 'movies' else 'show'
            remove_url = f'https://api.trakt.tv/sync/watchlist/remove'
            remove_data = {
                media_type + 's': [{'ids': {'trakt': int(params['remove'])}}]
            }
            
            response = _session.post(remove_url, headers=trakt_get_headers(write=True), json=remove_data, timeout=10)
            
            if response.status_code == 401:
                # Token might be expired - try to refresh
                if trakt_refresh_token():
                    response = _session.post(remove_url, headers=trakt_get_headers(write=True), json=remove_data, timeout=10)
            
            if response.status_code == 200:
                popinfo("Položka odstraněna z watchlistu", icon=xbmcgui.NOTIFICATION_INFO)
            elif response.status_code == 401:
                popinfo("Připojení k Traktu vypršelo", icon=xbmcgui.NOTIFICATION_ERROR)
            else:
                popinfo(f"Chyba při odstraňování: {response.status_code}", icon=xbmcgui.NOTIFICATION_ERROR)
            
            xbmc.executebuiltin('Container.Refresh()')
            return
        
        # Handle seasons listing for a show
        if 'show_id' in params and 'season' not in params:
            return list_seasons(params)
        
        # Handle episodes listing for a season
        if 'show_id' in params and 'season' in params:
            return list_episodes(params)
        
        # Fetch watchlist with images
        url = f'https://api.trakt.tv/users/me/watchlist/{params["category"]}?extended=full,images'
        response = _session.get(url, headers=trakt_get_headers(), timeout=10)
        
        response = handle_trakt_401(url)
        if not response or response.status_code != 200:
            return
        
        items = response.json()
        items = sorted(items, key=lambda x: x['movie']['title'] if 'movie' in x else x['show']['title'])
        
        for item in items:
            if params['category'] == 'movies' and 'movie' in item:
                media = item['movie']
                media_type = 'movie'
                media_id = media['ids']['trakt']
                
                # Try to get Czech translation
                try:
                    translation_url = f'https://api.trakt.tv/{media_type}s/{media_id}/translations/cs'
                    translation_response = _session.get(translation_url, headers=trakt_get_headers(), timeout=10)
                    if translation_response.status_code == 200:
                        translation = translation_response.json()
                        if translation and isinstance(translation, list):
                            title = translation[0].get('title', media.get('title', 'Neznámý název'))
                            plot = translation[0].get('overview', media.get('overview', ''))
                        else:
                            title = media.get('title', 'Neznámý název')
                            plot = media.get('overview', '')
                    else:
                        title = media.get('title', 'Neznámý název')
                        plot = media.get('overview', '')
                except Exception as e:
                    log(f"Chyba při načítání překladu: {str(e)}", xbmc.LOGERROR)
                    title = media.get('title', 'Neznámý název')
                    plot = media.get('overview', '')

                # Fallback to original title if translation fails
                if not title:
                    title = media.get('title', 'Neznámý název')
                
                # Add year if available
                year = media.get('year', '')
                if year:
                    title = f"{title} ({year})"
                
                artwork = {}
                if isinstance(media.get('images'), dict):
                    images = media['images']
                    if isinstance(images.get('poster'), list) and len(images['poster']) > 0:
                        poster_url = images['poster'][0]
                        artwork['poster'] = f"https://{poster_url}" if not poster_url.startswith('http') else poster_url
                    if isinstance(images.get('fanart'), list) and len(images['fanart']) > 0:
                        fanart_url = images['fanart'][0]
                        artwork['fanart'] = f"https://{fanart_url}" if not fanart_url.startswith('http') else fanart_url
                    artwork['thumb'] = artwork.get('poster', '')
                          
                # Create list item
                listitem = xbmcgui.ListItem(label=title)
                if artwork:
                    listitem.setArt(artwork)

                # Create context menu items
                context_menu_items = []
                
                if media.get('trailer'):
                    trailer_url = media['trailer']
                    if 'youtube.com' in trailer_url or 'youtu.be' in trailer_url:
                        video_id = trailer_url.split('v=')[-1].split('&')[0]
                        youtube_plugin_url = f'plugin://plugin.video.youtube/play/?video_id={video_id}'
                        context_menu_items.append((
                            "Přehrát trailer",
                            f'PlayMedia({youtube_plugin_url})'
                        ))    

                context_menu_items.append((
                    'Vyhledat původní název', 
                    f'Container.Update({get_url(action="search", what=media.get("title", ""))})'
                ))
                
                context_menu_items.append((
                    'Odstranit z watchlistu',
                    f'RunPlugin({get_url(action="trakt_watchlist", category=params["category"], remove=media_id)})'
                ))
                
                listitem.addContextMenuItems(context_menu_items)

                info = {
                    'title': title,
                    'mediatype': media_type,
                    'plot': plot,
                    'year': int(year) if year else 0,
                    'genre': " / ".join(media.get('genres', [])),
                    'duration': media.get('runtime', 0),
                    'trailer': media.get('trailer'),
                    'rating': float(media.get('rating', 0)),
                }
                listitem.setInfo('video', info)
                
                xbmcplugin.addDirectoryItem(
                    _handle,
                    get_url(action='search', what=title),
                    listitem,
                    True
                )
                
            elif params['category'] == 'shows' and 'show' in item:
                media = item['show']
                media_type = 'show'
                media_id = media['ids']['trakt']
                
                # Try to get Czech translation
                try:
                    translation_url = f'https://api.trakt.tv/{media_type}s/{media_id}/translations/cs'
                    translation_response = _session.get(translation_url, headers=trakt_get_headers(), timeout=10)
                    if translation_response.status_code == 200:
                        translation = translation_response.json()
                        if translation and isinstance(translation, list):
                            title = translation[0].get('title', media.get('title', 'Neznámý název'))
                            plot = translation[0].get('overview', media.get('overview', ''))
                        else:
                            title = media.get('title', 'Neznámý název')
                            plot = media.get('overview', '')
                    else:
                        title = media.get('title', 'Neznámý název')
                        plot = media.get('overview', '')
                except Exception as e:
                    log(f"Chyba při načítání překladu: {str(e)}", xbmc.LOGERROR)
                    title = media.get('title', 'Neznámý název')
                    plot = media.get('overview', '')

                # Fallback to original title if translation fails
                if not title:
                    title = media.get('title', 'Neznámý název')
                
                # Add year if available
                year = media.get('year', '')
                if year:
                    title = f"{title} ({year})"
                
                artwork = {}
                if isinstance(media.get('images'), dict):
                    images = media['images']
                    if isinstance(images.get('poster'), list) and len(images['poster']) > 0:
                        poster_url = images['poster'][0]
                        artwork['poster'] = f"https://{poster_url}" if not poster_url.startswith('http') else poster_url
                    if isinstance(images.get('fanart'), list) and len(images['fanart']) > 0:
                        fanart_url = images['fanart'][0]
                        artwork['fanart'] = f"https://{fanart_url}" if not fanart_url.startswith('http') else fanart_url
                    artwork['thumb'] = artwork.get('poster', '')
                          
                # Create list item
                listitem = xbmcgui.ListItem(label=title)
                if artwork:
                    listitem.setArt(artwork)

                # Create context menu items
                context_menu_items = []
                
                if media.get('trailer'):
                    trailer_url = media['trailer']
                    if 'youtube.com' in trailer_url or 'youtu.be' in trailer_url:
                        video_id = trailer_url.split('v=')[-1].split('&')[0]
                        youtube_plugin_url = f'plugin://plugin.video.youtube/play/?video_id={video_id}'
                        context_menu_items.append((
                            "Přehrát trailer",
                            f'PlayMedia({youtube_plugin_url})'
                        ))    

                context_menu_items.append((
                    'Vyhledat původní název', 
                    f'Container.Update({get_url(action="search", what=media.get("title", ""))})'
                ))
                
                context_menu_items.append((
                    'Odstranit z watchlistu',
                    f'RunPlugin({get_url(action="trakt_watchlist", category=params["category"], remove=media_id)})'
                ))
                
                listitem.addContextMenuItems(context_menu_items)

                info = {
                    'title': title,
                    'mediatype': media_type,
                    'plot': plot,
                    'year': int(year) if year else 0,
                    'genre': " / ".join(media.get('genres', [])),
                    'status': media.get('status', ''),
                    'rating': float(media.get('rating', 0)),
                }
                listitem.setInfo('video', info)
                
                # For shows, link to seasons listing
                xbmcplugin.addDirectoryItem(
                    _handle,
                    get_url(action='trakt_watchlist', show_id=media_id, category='shows'),
                    listitem,
                    True
                )
                
        # Add authentication item if not authenticated
        if not _addon.getSetting('trakt_access_token'):
            listitem = xbmcgui.ListItem(label="Připojit k Traktu...")
            listitem.setArt({'icon': 'DefaultAddonService.png'})
            xbmcplugin.addDirectoryItem(
                _handle,
                get_url(action='trakt_watchlist', reauth=1),
                listitem,
                False
            )
        
    except Exception as e:
        log(f"Trakt chyba: {str(e)}", xbmc.LOGERROR)
        popinfo("Chyba při načítání", icon=xbmcgui.NOTIFICATION_ERROR)
        traceback.print_exc()
        
    xbmcplugin.setContent(_handle, 'movies' if params.get('category') == 'movies' else 'tvshows')
    xbmcplugin.endOfDirectory(_handle)

@handle_errors
def list_seasons(params):
    """List all seasons for a show"""
    show_id = params['show_id']
    
    # Get show details
    show_url = f'https://api.trakt.tv/shows/{show_id}?extended=full,images'
    show_response = _session.get(show_url, headers=trakt_get_headers(), timeout=10)
    
    if show_response.status_code != 200:
        popinfo("Chyba při načítání detailu seriálu", icon=xbmcgui.NOTIFICATION_ERROR)
        return
    
    show = show_response.json()
    
    # Get Czech title if available
    try:
        translation_url = f'https://api.trakt.tv/shows/{show_id}/translations/cs'
        translation_response = _session.get(translation_url, headers=trakt_get_headers(), timeout=10)
        if translation_response.status_code == 200:
            translation = translation_response.json()
            if translation and isinstance(translation, list):
                title = translation[0].get('title', show.get('title', 'Neznámý název'))
            else:
                title = show.get('title', 'Neznámý název')
        else:
            title = show.get('title', 'Neznámý název')
    except Exception:
        title = show.get('title', 'Neznámý název')
    
    # Get all seasons
    seasons_url = f'https://api.trakt.tv/shows/{show_id}/seasons?extended=episodes'
    seasons_response = _session.get(seasons_url, headers=trakt_get_headers(), timeout=10)
    
    if seasons_response.status_code != 200:
        popinfo("Chyba při načítání sezón", icon=xbmcgui.NOTIFICATION_ERROR)
        return
    
    seasons = seasons_response.json()
    
    # Set plugin category to show title
    xbmcplugin.setPluginCategory(_handle, f"{_addon.getAddonInfo('name')} \\ {title}")
        
    # Add each season
    for season in sorted(seasons, key=lambda x: x['number']):
        season_num = season['number']
        episode_count = len(season.get('episodes', []))
        
        listitem = xbmcgui.ListItem(label=f"Sezóna {season_num} ({episode_count} epizod)")
        listitem.setArt({'icon': 'DefaultSeason.png'})
        
        # Add season info
        info = {
            'title': f"Sezóna {season_num}",
            'mediatype': 'season',
            'season': season_num,
            'episode': episode_count,
        }
        listitem.setInfo('video', info)
        
        xbmcplugin.addDirectoryItem(
            _handle,
            get_url(action='trakt_watchlist', show_id=show_id, season=season_num, series_title=show.get('title'),category='shows'),
            listitem,
            True
        )
    
    xbmcplugin.setContent(_handle, 'seasons')
    xbmcplugin.endOfDirectory(_handle)

@handle_errors
def list_episodes(params):
    """List all episodes for a season"""
    show_id = params['show_id']
    season_num = params['season']
    series_title = params['series_title']

    xbmcplugin.setPluginCategory(_handle, f"{_addon.getAddonInfo('name')} / Sezóna {season_num}")

    trakt_client_id = _addon.getSetting('trakt_client_id').strip()
    if not trakt_client_id:
        popinfo("Pro připojení k Traktu je třeba vyplnit Client ID v nastavení.", sound=True)
        _addon.openSettings()
        xbmcplugin.endOfDirectory(_handle)
        return

    token = revalidate()

    # Načtení epizod ze sezóny
    url = f"https://api.trakt.tv/shows/{show_id}/seasons/{season_num}?extended=episodes"
    response = _session.get(url, headers=trakt_get_headers(), timeout=10)

    response = handle_trakt_401(url)
    if not response or response.status_code != 200:
        return

    episodes = response.json()

    today = datetime.today().date()

    for episode in episodes:
        ep_num = episode.get('number')
        ep_title = episode.get('title') or 'Neznámý název'
        ep_air_date = episode.get('first_aired')
        ep_plot = episode.get('overview') or ''

        # Zkusit načíst český překlad
        try:
            translation_url = f"https://api.trakt.tv/shows/{show_id}/seasons/{season_num}/episodes/{ep_num}/translations/cs"
            translation_response = _session.get(translation_url, headers=trakt_get_headers(), timeout=10)
            if translation_response.status_code == 200:
                translations = translation_response.json()
                if translations and isinstance(translations, list):
                    ep_title = translations[0].get('title', ep_title)
                    ep_plot = translations[0].get('overview', ep_plot)
        except Exception as e:
            log(f"Chyba při načítání překladu epizody: {str(e)}", xbmc.LOGERROR)

        # Kontrola, jestli epizoda ještě nebyla odvysílána
        is_future = False
        if ep_air_date:
            try:
                air_date_obj = datetime.strptime(ep_air_date[:10], "%Y-%m-%d").date()
                if air_date_obj > today:
                    is_future = True
            except Exception as e:
                log(f"Chyba při parsování data: {str(e)}", xbmc.LOGERROR)

        # Připravit label
        label = f"Sezóna {season_num}, Epizoda {ep_num}: {ep_title}"
        if is_future:
            label = f"[COLOR red]{label}[/COLOR]"

        listitem = xbmcgui.ListItem(label=label)
        listitem.setInfo('video', {
            'title': ep_title,
            'plot': ep_plot,
            'season': int(season_num),
            'episode': int(ep_num)
        })

        xbmcplugin.addDirectoryItem(
            _handle,
            get_url(action='search', what=f"{series_title} S{int(season_num):02d}E{int(ep_num):02d}"),
            listitem,
            True
        )

    xbmcplugin.addSortMethod(_handle, xbmcplugin.SORT_METHOD_EPISODE)
    xbmcplugin.endOfDirectory(_handle)


@handle_errors
def trakt_authenticate():
    """Handle Trakt OAuth authentication using device flow"""
    trakt_client_id = _addon.getSetting('trakt_client_id').strip()
    trakt_client_secret = _addon.getSetting('trakt_client_secret').strip()
    
    if not trakt_client_id or not trakt_client_secret:
        popinfo("Pro připojení k Traktu je třeba vyplnit Client ID a Client Secret v nastavení.", sound=True)
        _addon.openSettings()
        return False
    
    # Step 1: Get device code
    data = {
        'client_id': trakt_client_id
    }
    
    try:
        response = _session.post(TRAKT_DEVICE_CODE_URL, data=data, timeout=30)
        response.raise_for_status()
        device_data = response.json()
        
        # Show user the verification info
        dialog = xbmcgui.Dialog()
        dialog.textviewer(
            'Trakt ověření',
            f"1. Otevřete v prohlížeči: [B]{device_data['verification_url']}[/B]\n"
            f"2. Zadejte tento kód: [B]{device_data['user_code']}[/B]\n\n"
            f"Kód platí {device_data['expires_in']//60} minut.\n"
            "Po ověření zmáčkněte ESC/zpět."
        )
        
        # Step 2: Poll for token
        data = {
            'client_id': trakt_client_id,
            'client_secret': trakt_client_secret,
            'code': device_data['device_code']
        }
        
        interval = device_data['interval']
        expires_in = device_data['expires_in']
        start_time = time.time()
        
        progress = xbmcgui.DialogProgress()
        progress.create('Trakt ověření', 'Čekání na uživatelské ověření...')
        
        while (time.time() - start_time) < expires_in:
            if progress.iscanceled():
                break
                
            progress.update(int(((time.time() - start_time) / expires_in) * 100))
            
            try:
                response = _session.post(TRAKT_DEVICE_TOKEN_URL, data=data, timeout=30)
                
                if response.status_code == 200:
                    token_data = response.json()
                    _addon.setSetting('trakt_access_token', token_data['access_token'])
                    _addon.setSetting('trakt_refresh_token', token_data['refresh_token'])
                    progress.close()
                    popinfo("Úspěšně připojeno k Traktu!")
                    return True
                
                elif response.status_code == 400:
                    # Still pending - wait and try again
                    time.sleep(interval)
                
                else:
                    response.raise_for_status()
                    
            except Exception as e:
                log(f"Trakt authentication error: {str(e)}", xbmc.LOGERROR)
                break
        
        progress.close()
        popinfo("Čas na ověření vypršel nebo došlo k chybě.", icon=xbmcgui.NOTIFICATION_ERROR)
        
    except Exception as e:
        log(f"Trakt authentication failed: {str(e)}", xbmc.LOGERROR)
        popinfo("Chyba při připojování k Traktu", icon=xbmcgui.NOTIFICATION_ERROR)
    
    return False

@handle_errors
def trakt_refresh_token():
    trakt_client_id = _addon.getSetting('trakt_client_id').strip()
    trakt_client_secret = _addon.getSetting('trakt_client_secret').strip()
    trakt_refresh_token = _addon.getSetting('trakt_refresh_token').strip()

    if not all([trakt_client_id, trakt_client_secret, trakt_refresh_token]):
        log("Chybí údaje pro refresh tokenu", xbmc.LOGERROR)
        return False

    data = {
        'client_id': trakt_client_id,
        'client_secret': trakt_client_secret,
        'refresh_token': trakt_refresh_token,
        'grant_type': 'refresh_token',
        'redirect_uri': 'urn:ietf:wg:oauth:2.0:oob'
    }

    try:
        response = _session.post(TRAKT_TOKEN_URL, data=data, timeout=30)
        log(f"Refresh token response: {response.status_code} - {response.text}", xbmc.LOGDEBUG)

        if response.status_code == 401:
            # Kompletně neplatné údaje - nutná nová autentizace
            _addon.setSetting('trakt_access_token', '')
            _addon.setSetting('trakt_refresh_token', '')
            popinfo("Přihlášení vypršelo, proveďte novou autentizaci", icon=xbmcgui.NOTIFICATION_WARNING)
            return False

        response.raise_for_status()
        token_data = response.json()

        _addon.setSetting('trakt_access_token', token_data['access_token'])
        _addon.setSetting('trakt_refresh_token', token_data['refresh_token'])
        log("Token úspěšně obnoven", xbmc.LOGINFO)
        return True

    except Exception as e:
        log(f"CHYBA při refreshi tokenu: {str(e)}", xbmc.LOGERROR)
        return False

def trakt_get_headers(write=False):
    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': _addon.getSetting('trakt_client_id').strip(),
        'Accept-Language': 'cs'
    }

    log(f"Trakt headers: {_addon.getSetting('trakt_access_token')}", xbmc.LOGDEBUG)
    
    if write:
        access_token = _addon.getSetting('trakt_access_token').strip()
        if not access_token:
            # Pokus o obnovení tokenu, pokud chybí
            trakt_refresh_token()
            access_token = _addon.getSetting('trakt_access_token').strip()
        
        if access_token:
            headers['Authorization'] = f'Bearer {access_token}'
        else:
            log("Chybí Trakt access token!", xbmc.LOGERROR)
    
    return headers

def handle_trakt_401(url, method='GET', data=None):
    """Specializovaný handler pro 401 chyby"""
    for attempt in range(2):  # 2 pokusy (1x refresh + 1x nový pokus)
        headers = trakt_get_headers(write=True)
        response = _session.request(
            method,
            url,
            headers=headers,
            json=data,
            timeout=15
        )
        
        log(f"Trakt API attempt {attempt}: {response.status_code}", xbmc.LOGDEBUG)
        
        if response.status_code != 401:
            return response
            
        if not trakt_refresh_token():
            break  # Refresh se nepovedl
    
    # Pokud jsme se sem dostali, selhalo vše
    popinfo("Vyžaduje nové přihlášení k Traktu", icon=xbmcgui.NOTIFICATION_WARNING)
    trakt_authenticate()
    return None


if __name__ == '__main__':
    router(sys.argv[2][1:])