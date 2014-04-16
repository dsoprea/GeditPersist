import logging
import shutil
import os
import os.path
import json

from datetime import datetime
from os import environ, makedirs, unlink, listdir, rmdir
from os.path import expanduser, join, exists

import gi.repository

from gi.repository import GObject, Gedit, Gio
from gi.repository.Gio import Settings

_log = logging.getLogger(__name__)

# TODO(dustin): Do we want to disable the "save before exit" dialog?
# TODO(dustin): We need to add a new menuitem to "close all windows".

#if environ.get('DEBUG', '').lower() == 'true':
#    _log.setLevel(logging.DEBUG)
_log.setLevel(logging.DEBUG)

# For some reason our logging won't emit correctly unless an initial message is
# sent in.
logging.debug("")

_SETTINGS_KEY = "org.gnome.gedit.preferences.editor"
_PREF_DIR_NAME = '.gedit-sessions'
_SESSION_STATE_FILENAME = 'state.json'

_gedit_settings = Settings(_SETTINGS_KEY)

#self.filename = os.path.join(GLib.get_user_config_dir(), 'gedit/saved-sessions.xml')


class PersistPluginApp(GObject.Object, Gedit.AppActivatable):
    __gtype_name__ = 'PersistPluginApp'
    app = GObject.property(type=Gedit.App)

    def __init__(self):
        GObject.Object.__init__(self)

        pref_path = os.path.join(os.path.expanduser('~'), _PREF_DIR_NAME)
        self.__staging_path = os.path.join(pref_path, 'staging')
        self.__session_path = os.path.join(pref_path, 'session')
        self.__backup_path = os.path.join(pref_path, 'backup')


# TODO(dustin): What happens when a setting isn't defined? Catch and set to
#               another value.

        #self.__capture_interval_s = _gedit_settings.get_uint(
        #                                'auto-save-interval') * 60
        self.__capture_interval_s = 10

        self.__timer_capture = None
        self.__timer_recall = None

    def __schedule_capture(self):
        self.__timer_capture = GObject.timeout_add_seconds(
                                self.__capture_interval_s, 
                                self.__capture_cb)

    def __capture_cb(self):
# TODO(dustin): We can use keypresses to determine if a save needs to be done.

        _log.debug("Capturing current session.")

        n = 0
        dsf = Gedit.DocumentSaveFlags(n)

        if os.path.exists(self.__staging_path) is True:
            shutil.rmtree(self.__staging_path)

        os.makedirs(self.__staging_path)

        windows = self.app.get_windows()
        i = 0
        meta_app = []
        for window in windows:
            rel_window_path = str(i)
            window_path = os.path.join(self.__staging_path, rel_window_path)
            os.mkdir(window_path)
        
            j = 0
            meta_window = []
            for document in window.get_documents():

                # Skip an unused, empty document.
                if document.is_untouched() is True:
                    continue

                if document.is_untitled() is True:
                    is_stored = False
                    rel_doc_filepath = os.path.join(rel_window_path, str(j))
                    staging_doc_filepath = os.path.join(self.__staging_path, 
                                                        rel_doc_filepath)
                    doc_filepath = os.path.join(self.__session_path,
                                                        rel_doc_filepath)

                    _log.debug("Writing untitled file to temporary location: "
                               "%s" % (staging_doc_filepath))

                    with open(staging_doc_filepath, 'w') as f:
                        line_it_start = document.get_start_iter()
                        k = 0
                        while 1:
                            line_it_end = line_it_start.copy()
                            if line_it_end.forward_line() is False:
                                line_it_end = document.get_end_iter()

                            line_text = line_it_start.get_text(line_it_end)
                            f.write(line_text)

                            line_it_start = line_it_end
                            if line_it_end.is_end() is True:
                                break
                                
                            k += 1
                else:
                    is_stored = True

                    location = document.get_location()
                    doc_filepath = location.get_parse_name()

# TODO(dustin): Verify that this works.
                    tab = Gedit.Tab.get_from_document(document)
                    state = tab.get_state()

                    # Skipped any document that hasn't been successfully 
                    # loaded.
                    if state != state.STATE_NORMAL:
                        _log.warning("Skipping file [%s] with abnormal state: "
                                     "%s" % (doc_filepath, state))
                        continue

                    if document.get_modified() is True:
                        _log.debug("Saving modified stored file: %s" % 
                                   (doc_filepath))
                        document.save(dsf)

                # Grab line-number, too.

                insert_mark = document.get_insert()
                offset = document.get_iter_at_mark(insert_mark)
                current_line = offset.get_line()

                encoding = document.get_encoding()

# TODO(dustin): Capture the currently-active tab (this should be a simple flag 
#               on the tab).
                meta_window.append({
                        'encoding': encoding.get_name(),
                        'line': current_line,
                        'was_stored': is_stored,
                        'filepath': doc_filepath,
                    })

                j += 1

            meta_app.append(meta_window)
            i += 1

        meta_filepath = os.path.join(self.__staging_path, 
                                     _SESSION_STATE_FILENAME)

        _log.debug("Writing meta file-path: %s" % (meta_filepath))
        with open(meta_filepath, 'w') as f:
            json.dump(meta_app, f)

        if os.path.exists(self.__backup_path) is True:
            shutil.rmtree(self.__backup_path)

        if os.path.exists(self.__session_path) is True:
            shutil.move(self.__session_path, self.__backup_path)
    
        shutil.move(self.__staging_path, self.__session_path)

        self.__schedule_capture()

    def __schedule_ready_check(self):
        self.__timer_recall = GObject.timeout_add_seconds(
                                1, 
                                self.__wait_until_ready_cb)

    def __wait_until_ready_cb(self):
        active_window = self.app.get_active_window()
        if active_window is None:
            _log.debug("Not Ready (1): No active window.")
            self.__schedule_ready_check()
            return

        active_state = active_window.get_state()
        if active_state != active_state.NORMAL:
            _log.debug("Not Ready (2): State: %s" % (active_state))
            self.__schedule_ready_check()
            return

        _log.debug("App is in ready state.")

        # Try to load (recall) the last state.
        _log.debug("Finding/recalling previous session.")
        self.__try_recall()

        # Schedule periodic saves.
        _log.debug("Scheduling captures.")
        self.__schedule_capture()

    def __create_untitled_tab(self, window, rows, line):
        tab = window.create_tab(False)
        d = tab.get_document()

        it = d.get_start_iter()
        b = it.get_buffer()

        for row in rows:
            b.insert_at_cursor(row)

        d.place_cursor(d.get_iter_at_line(line))

    def __create_window(self):
        window = self.app.create_window(None)
        window.set_visible(True)

        return window

    def __try_recall(self):
        _log.debug("Looking for existing sessions: %s" % (self.__session_path))

        if os.path.exists(self.__session_path) is False:
            _log.debug("Session path not found.")    
            return

        meta_filepath = os.path.join(self.__session_path, 
                                     _SESSION_STATE_FILENAME)

        _log.debug("Loading session state.")

        try:
            with open(meta_filepath) as f:
                meta_app = json.load(f)
        except IOError:
            _log.exception("The session directory exists, but no meta-file "
                           "was found. Skipping.")
            return

        # Establish a list of documents already loaded (Gedit may or may not 
        # have loaded files passed as arguments). Gedit will not try to prevent 
        # double-loading, so we'll try.

        current = []
        for d in self.app.get_documents():
            l = d.get_location()
            if l is not None:
                current.append(l.get_parse_name())
        
        for i in xrange(len(meta_app)):
            doc_list = meta_app[i]
            _log.debug("Populating window (%d): (%d) tab(s)" % 
                       (i, len(doc_list)))

            # Get a list of documents that aren't already loaded.
            distilled_doc_list = filter(
                lambda di: di['was_stored'] is False or \
                           di['filepath'] not in current, 
                doc_list)

            if not distilled_doc_list:
                continue

            # Populate documents.

            window = self.__create_window()
            for j in xrange(len(distilled_doc_list)):
                document_info = distilled_doc_list[j]
                _log.debug("Installing document: %s" % (document_info,))

                encoding = Gedit.Encoding.get_from_charset(
                                            document_info['encoding'])

                file_path = document_info['filepath']
                if document_info['was_stored'] is True:
                    # This document came from a physical file, originally.

                    location = Gio.file_parse_name(file_path)
                    tab = window.create_tab_from_location(
                            location, 
                            encoding, 
                            document_info['line'], 
                            0, 
                            False, 
                            True)
                else:
                    # This document came from an untitled document, originally.

                    with open(os.path.join(self.__session_path, file_path)) as f:
                        tab = self.__create_untitled_tab(
                                window, 
                                f, 
                                document_info['line'])

    def do_activate(self):

        # Wait until the app enters the normal (ready) state.
        _log.debug("Waiting for app to enter ready state.")
        self.__schedule_ready_check()

    def do_deactivate(self):
        if self.__timer_capture is not None:
            GObject.source_remove(self.__timer_capture)
            del self.__timer_capture

        if self.__timer_recall is not None:
            GObject.source_remove(self.__timer_recall)
            del self.__timer_recall


class PersistPluginWindow(GObject.Object, Gedit.WindowActivatable):
    __gtype_name__ = 'PersistPluginWindow'

    window = GObject.property(type=Gedit.Window)

    def __init__(self):
        GObject.Object.__init__(self)

    def do_activate(self):
        _log.debug("Creating window.")

    def do_deactivate(self):
        pass

    def do_update_state(self):
        pass

