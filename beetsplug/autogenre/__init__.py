import codecs
import os
import re
import yaml
from collections import Counter
from beets.plugins import BeetsPlugin
from beets.dbcore import types
from beets.library import Item, parse_query_parts
from beets.ui import Subcommand
from beets import config
from optparse import OptionParser
from confuse import ConfigSource, load_yaml
from beetsplug import lastgenre
from beetsplug.lastgenre import LastGenrePlugin
from beetsplug.xtractor import XtractorCommand
from beetsplug.autogenre.genretree import GenreTree

try:
    from beets.dbcore.sort import FixedFieldSort
except ImportError: # beets < 2.11
    from beets.dbcore.query import FixedFieldSort

# See https://essentia.upf.edu/svm_models/accuracies_v2.1_beta1.html
ROSAMERICA_GENRES = {
    'cla': 'classical',
    'dan': 'dance', # CAUTION: 'dance' is an out-of-tree genre: might be electronic or rock. better set genre of those tracks manually.
    'hip': 'hip hop',
    'jaz': 'jazz',
    'pop': 'pop',
    'rhy': 'rhythm and blues',
    'roc': 'rock',
    'spe': 'speech'
}
ELECTRONIC_GENRES = {
    'ambient': 'ambient',
    'dnb': 'drum and bass',
    'house': 'house',
    'techno': 'techno',
    'trance': 'trance',
}
SOURCES = set(('lastfm', 'title', 'essentia', 'user'))

class AutoGenrePlugin(BeetsPlugin):
    # The genres themselves are stored within beets' native `genres` list field.
    item_types = {
        'genre_source': types.STRING,
    }

    @property
    def album_types(self):
        return self.item_types

    def __init__(self):
        super(AutoGenrePlugin, self).__init__()
        config_file_path = os.path.join(os.path.dirname(__file__), 'config_default.yaml')
        source = ConfigSource(load_yaml(config_file_path) or {}, config_file_path)
        self.config.add(source)
        assert _is_plugin_enabled('lastgenre'), "The 'lastgenre' plugin is not enabled!"
        assert _is_plugin_enabled('xtractor'), "The 'xtractor' plugin is not enabled!"
        self._lastgenre = LastGenrePlugin()
        self._xtractor_cmd = None
        self._remix_regex = re.compile(r'.+[^\w](remix|bootleg|remake)', re.IGNORECASE)
        self._genre_tree = None
        if self.config['auto'].get(bool):
            self.import_stages = [self.imported]

    def imported(self, session, task):
        """Event hook called when an import task finishes."""
        if task.is_album:
            for item in task.album.items():
                self._update_item_genre(item)

            self._update_album_genre(task.album)
        else:
            self._update_item_genre(task.item)

    def _update_item_genre(self, item):
        genres, source = self._item_genre(item, True, True)
        self._log.info("Set track genres '{}' ({}): {}", _genres2str(genres), source, item)
        item.genres = genres
        item.genre_source = source
        if not self.config['pretend'].get():
            if config['import']['write'].get():
                item.try_write()
            item.store()

    def _update_album_genre(self, album):
        item_genres = [tuple(item.genres) for item in album.items() if item.genres]
        genre = _most_common([genres[0] for genres in item_genres])
        genres = list(_most_common([g for g in item_genres if g[0] == genre]) or [])
        if list(album.genres) != genres and genres:
            album.genres = genres
            self._log.info("Set genres '{}' for album {}", _genres2str(genres), album)
            if not self.config['pretend'].get():
                # Don't overwrite the genres of the album's tracks.
                album.store(['genres'], inherit=False)

    def commands(self):
        p = OptionParser()
        p.add_option('--pretend', action='store_true',
            default=self.config['pretend'].get(),
            help='do not persist item changes but log them')
        p.add_option('-f', '--force', action='store_true',
            default=self.config['force'].get(),
            help='reevaluate genres for items with a matching genre_source')
        p.add_option('-a', '--all', action='store_true',
            default=self.config['all'].get(),
            dest='all', help='overwrite genre if genre_source not specified')
        p.add_option('--no-all', action='store_false',
            default=self.config['all'].get(),
            dest='all', help='do not overwrite genre from unspecified source')
        p.add_option('--lastgenre', action='store_true',
            default=self.config['lastgenre'].get(),
            dest='lastgenre', help='use lastgenre plugin')
        p.add_option('--no-lastgenre', action='store_false',
            default=self.config['lastgenre'].get(),
            dest='lastgenre', help='do not use lastgenre plugin')
        p.add_option('--xtractor', action='store_true',
            default=self.config['xtractor'].get(),
            dest='xtractor', help='use xtractor plugin')
        p.add_option('--no-xtractor', action='store_false',
            default=self.config['xtractor'].get(),
            dest='xtractor', help='do not use xtractor plugin')
        p.add_option('--from-title', action='store_true',
            default=self.config['from_title'].get(),
            dest='from_title', help='derive genre from title')
        p.add_option('--no-from-title', action='store_false',
            default=self.config['from_title'].get(),
            dest='from_title', help='do not derive genre from title')
        p.add_option('--parent-genres', action='store_true',
            default=self.config['parent_genres'].get(),
            dest='parent_genres', help="add primary genre's parent genres")
        p.add_option('--no-parent-genres', action='store_false',
            default=self.config['parent_genres'].get(),
            dest='parent_genres', help="do not add primary genre's parent genres")
        p.add_option('--genre', type='string',
            dest='genre', help='specify the genre to assign to the selected items')

        c = Subcommand('autogenre', parser=p, help='derive and assign song genres')
        c.func = self._run_autogenre_cmd
        return [c]

    def _run_autogenre_cmd(self, lib, opts, args):
        self._apply_opts_to_config(opts)
        if opts.genre:
            ok = self._genres().contains(opts.genre)
            assert args, "Must specify selector when --genre provided"
            assert ok, "Provided genre '{}' is not registered within genre tree!".format(opts.genre)
        parsed_query, parsed_sort = parse_query_parts(args, Item)
        parsed_sort = FixedFieldSort("albumartist", ascending=True)
        items = lib.items(parsed_query, parsed_sort)
        all = opts.all or opts.genre is not None
        force = opts.force or opts.genre is not None
        pretend = self.config['pretend'].get()
        filtered_items = [item for item in items if _filter_item(item, all, force)]
        self._log.info('Selected {} items for genre update...', len(filtered_items))
        # Update items
        for item in filtered_items:
            genres, source = self._item_genre(item, all, force, opts.genre)
            old_genres = list(item.get('genres') or [])
            genres_changed = genres != old_genres
            genre_source_changed = source != item.get('genre_source')
            changed = genres_changed or genre_source_changed
            if changed and genres:
                msg = "Change genres from '{}' to '{}' ({}) for item: {}"
                self._log.info(msg, _genres2str(old_genres), _genres2str(genres), source, item)
                if not pretend:
                    item.genres = genres
                    item.genre_source = source
                    if config['import']['write'].get():
                        item.try_write()
                    item.store()
        # TODO: match remix artist within title and get genre from artist: TITLE (ARTIST remix)
        # Update albums
        album_ids = set([item.album_id for item in filtered_items if item.album_id])
        for album_id in album_ids:
            album = lib.get_album(album_id)
            if album:
                self._update_album_genre(album)

    def _apply_opts_to_config(self, opts):
        for k, v in opts.__dict__.items():
            if v is not None and k in self.config:
                self.config[k] = v

    def _item_genre(self, item, all, force, force_genre=None):
        '''Returns the item's genre list (primary genre first) and its source.'''
        genres, source = self._item_genres(item, all, force, force_genre)
        genres = _unique([g for g in genres if g])

        if genres and self.config['parent_genres'].get():
            # Append primary genre's parent genres to genre list
            parent_genres = self._genres().parents(genres[0])
            parent_genres = [self._format_genre(g) for g in parent_genres]
            genres = _unique(genres + parent_genres)

        return genres, source

    def _item_genres(self, item, all, force, force_genre):
        genres = list(item.get('genres') or [])
        source = item.get('genre_source')
        if _filter_item(item, all, force):
            if force_genre is not None:
                source = force_genre and 'user' or None
                genres = force_genre and [self._format_genre(force_genre.lower())] or []
            if source != 'user' or not genres:
                # auto-detect genre
                if self.config['lastgenre'].get():
                    genres = self._lastfm_genre(item)
                    if genres:
                        source = 'lastfm'
                if self.config['from_title'].get():
                    genres, matched = self._fix_remix_genre(item, genres)
                    if matched and genres:
                        source = 'title'
                if not genres and self.config['xtractor'].get():
                    genres = self._essentia_genre(item)
                    if genres:
                        source = 'essentia'
        return genres, source

    def _is_remix(self, title):
        return self._remix_regex.match(title) is not None

    def _lastfm_genre(self, item):
        # lastgenre keeps the existing genres unless its force option is enabled.
        # Hide them since the item was selected to get its genres reevaluated.
        existing_genres = item.genres
        item.genres = []
        try:
            genres, src = self._lastfm_genre_lookup(item)
        finally:
            item.genres = existing_genres
        if genres:
            msg = "Got last.fm genres '{}' based on {} for item: {}"
            self._log.debug(msg, _genres2str(genres), src, item)
        return list(genres or [])

    def _lastfm_genre_lookup(self, item):
        source = self._lastgenre.config['source'].get()
        if self._is_remix(item.get('title')) and source != 'track':
            # When item is remix, get genre frpm the last.fm track.
            # (The artist's genre would be most likely wrong / the original.
            # E.g. 'Fugees - Ready or not (Champion Bootleg)'.
            # For other items the album/artist source is more reliable.)
            self._lastgenre.config['source'] = 'track'
            try:
                return self._lastgenre._get_genre(item)
            finally:
                self._lastgenre.config['source'].set(source)
        return self._lastgenre._get_genre(item)

    def _fix_remix_genre(self, item, genres):
        '''Match genre within title or album and prepend to genre list.
        This fixes remixes that are wrongly tagged on last.fm'''
        title = item.get('title')
        album = item.get('album')
        matched = self._genres().match(title)
        if matched:
            source = 'title'
        elif album:
            source = 'album'
            matched = self._genres().match(album)
        if matched:
            genres = _unique([self._format_genre(matched.lower())] + genres)
            self._log.debug("Fixed genres '{}' based on {} of item: {}", _genres2str(genres), source, item)
            return genres, True
        return genres, False

    def _xtractor(self):
        # Created lazily since the xtractor plugin may be loaded after this one,
        # in which case its default config is not available within __init__.
        if not self._xtractor_cmd:
            self._xtractor_cmd = XtractorCommand(config['xtractor'])
        return self._xtractor_cmd

    def _essentia_genre(self, item):
        # Prefix the xtractor plugin prepends to its fields (xtractor.field_prefix).
        # Older xtractor versions don't support it.
        prefix = getattr(self._xtractor(), 'cfg_field_prefix', None) or ''
        if not item.get(prefix + 'bpm') or not item.get(prefix + 'genre_rosamerica'):
            # Run essentia analysis if result not known yet.
            self._log.debug('Analyzing item using essentia: {}', item)
            self._xtractor()._run_analysis(item)
        if prefix + 'genre_rosamerica' not in item:
            # Essentia analysis may not provide data in some cases.
            return []
        # Use Essentia's mapped genre_rosamerica value
        genre_rosamerica = item[prefix + 'genre_rosamerica']
        genre_rosamerica_probability = float(item[prefix + 'genre_rosamerica_probability'])
        genre_electronic = item[prefix + 'genre_electronic']
        genre_electronic_probability = float(item[prefix + 'genre_electronic_probability'])
        genre = ROSAMERICA_GENRES.get(genre_rosamerica)
        genres = genre and [genre] or []
        genre_electronic_strong = self.config['genre_electronic_strong'].get()
        genre_rosamerica_strong = self.config['genre_rosamerica_strong'].get()
        genre_electronic_prepend = self.config['genre_electronic_prepend'].get()
        genre_electronic_append = self.config['genre_electronic_append'].get()

        if genre == 'dance':
            if genre_electronic_probability > genre_electronic_strong:
                # Use the result of Essentia's electronic genre model
                genre = ELECTRONIC_GENRES.get(genre_electronic)
                genres = genre and [genre] or []
        elif genre_rosamerica_probability < genre_rosamerica_strong and genre_electronic_probability > genre_electronic_prepend:
            # Prepend electronic to genre list
            genres = ['electronic'] + genres
            genre_electro = ELECTRONIC_GENRES.get(genre_electronic)
            if genre_electro: # Append electronic sub genre to list
                genres += [genre_electro]

        if genre_electronic_probability > genre_electronic_append:
            if genre_rosamerica in ('rhy', 'pop', 'hip'):
                # Append electronic to genre list
                if 'electronic' not in genres:
                    genres = genres + ['electronic']
            elif genre == 'dance':
                genres = ['electronic']

        genres = [self._format_genre(g) for g in genres]
        self._log.debug("Got essentia genres '{}' for item: {}", _genres2str(genres), item)
        return genres

    def _genres(self):
        if not self._genre_tree:
            genre_tree_file = self._lastgenre.config['canonical'].get()
            if not isinstance(genre_tree_file, str) or not genre_tree_file:
                # Use the genre tree that is shipped with the lastgenre plugin.
                genre_tree_file = os.path.join(os.path.dirname(lastgenre.__file__), 'genres-tree.yaml')
            genre_whitelist = self._lastgenre.whitelist
            assert genre_whitelist, "Config option lastgenre.whitelist is disabled!"
            with codecs.open(os.path.expanduser(genre_tree_file), 'r', encoding='utf-8') as f:
                genre_tree_yaml = yaml.safe_load(f)
            self._genre_tree = GenreTree(genre_tree_yaml, genre_whitelist)

        return self._genre_tree

    def _format_genre(self, genre):
        # Corresponds to the formatting the lastgenre plugin applies.
        return genre.title() if self._lastgenre.config['title_case'].get() else genre


def _filter_item(item, all, force):
    src = item.get('genre_source')
    empty = not item.get('genres')
    return (empty or src in SOURCES or not src and all) and (empty or force)

def _is_plugin_enabled(plugin_name):
    enabled_plugins = config['plugins'].get() if config['plugins'].exists() else []
    return plugin_name in enabled_plugins

def _most_common(names):
    r = Counter([name for name in names if name]).most_common(1)
    if len(r) == 1:
        return r[0][0]

def _unique(genres):
    '''Removes duplicate genres case-insensitively, preserving the order.'''
    r = {}
    for genre in genres:
        r.setdefault(genre.lower(), genre)
    return list(r.values())

def _genres2str(genres):
    return ', '.join(genres or [])
