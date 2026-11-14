"""MPRIS and local output routing checks; no real playback is controlled."""
import json
from types import SimpleNamespace
import subprocess
import unittest

from music_playback import MusicPlayback, PLAYER_INTERFACE, PLAYER_PREFIX, parse_gvariant


def variant(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, dict):
        return '{' + ', '.join(repr(key) + ': <' + variant(item) + '>' for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(variant(item) for item in value) + ']'
    if isinstance(value, int):
        return 'int64 ' + str(value)
    return str(value)


def properties(title='Test Song', url='https://music.apple.com/us/song/example/42', **extra):
    return {'Metadata': {'mpris:trackid': '/com/apple/music/track/42', 'mpris:length': 180_000_000,
                         'xesam:title': title, 'xesam:artist': ['Artist One', 'Artist Two'],
                         'xesam:album': 'Album', 'xesam:url': url},
            'PlaybackStatus': 'Playing', 'Position': 35_500_000, 'Volume': 0.4,
            'CanControl': True, 'CanPlay': True, 'CanPause': True,
            'CanGoNext': True, 'CanGoPrevious': False, 'CanSeek': True, **extra}


class FakeDesktop:
    def __init__(self, players=None):
        self.players = players if players is not None else {PLAYER_PREFIX + 'adam': properties()}
        self.commands = []
        self.mutations = []
        self.timeout = False
        self.sinks = [{'index': 10, 'name': 'speakers', 'description': 'Speakers'},
                      {'index': 20, 'name': 'headphones', 'description': 'Headphones'}]
        self.streams = [{'index': 100, 'sink': 10, 'properties': {'application.process.id': '900',
                                                                  'application.name': 'Adam'}},
                        {'index': 200, 'sink': 10, 'properties': {'application.process.id': '901',
                                                                  'application.name': 'Spotify'}}]

    def finder(self, program):
        return '/usr/bin/' + program

    def run(self, command, **kwargs):
        self.commands.append(command)
        assert kwargs['timeout'] <= 5
        assert kwargs['capture_output'] and kwargs['text'] and not kwargs['check']
        assert 'shell' not in kwargs
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        program, args = command[0].rsplit('/', 1)[-1], command[1:]
        if program == 'pactl':
            if args == ['-f', 'json', 'list', 'sinks']:
                result = json.dumps(self.sinks)
            elif args == ['-f', 'json', 'list', 'sink-inputs']:
                result = json.dumps(self.streams)
            elif args == ['get-default-sink']:
                result = 'speakers'
            else:
                self.mutations.append(command)
                result = ''
        else:
            method = args[args.index('--method') + 1]
            values = args[args.index('--method') + 2:]
            destination = args[args.index('--dest') + 1]
            if method.endswith('.ListNames'):
                result = '(' + variant(list(self.players) + [PLAYER_PREFIX + 'playerctld']) + ',)'
            elif method.endswith('.GetConnectionUnixProcessID'):
                result = '(uint32 900,)'
            elif method.endswith('.GetAll'):
                result = '(' + variant(self.players[destination] if values[0] == PLAYER_INTERFACE
                                       else {'Identity': destination[len(PLAYER_PREFIX):].capitalize()}) + ',)'
            else:
                self.mutations.append(command)
                result = '()'
        return SimpleNamespace(returncode=0, stdout=result, stderr='')


class MusicPlaybackTests(unittest.TestCase):
    def make(self, fake=None, **kwargs):
        desktop = fake or FakeDesktop()
        return MusicPlayback(runner=desktop.run, finder=desktop.finder, **kwargs), desktop

    def test_variant_types_nested_metadata_and_escaped_text(self):
        raw = "({'title': <'It\\'s 中文'>, 'length': <int64 123>, 'artists': <@as []>, 'flags': <[true, false]>},)"
        self.assertEqual(parse_gvariant(raw), [{'title': "It's 中文", 'length': 123,
                                               'artists': [], 'flags': [True, False]}])
        with self.assertRaises(ValueError):
            parse_gvariant("(__import__('os').system('bad'),)")
        with self.assertRaises(ValueError):
            parse_gvariant("({'title': <'missing terminator'},)")

    def test_adam_snapshot_reports_track_seconds_and_capabilities(self):
        client, desktop = self.make()
        state = client.snapshot()
        self.assertTrue(state['available'])
        self.assertEqual(state['player'], PLAYER_PREFIX + 'adam')
        self.assertEqual(state['track']['artist'], 'Artist One, Artist Two')
        self.assertEqual(state['track']['duration'], 180)
        self.assertEqual(state['position'], 35.5)
        self.assertEqual(state['volume'], .4)
        self.assertTrue(state['capabilities']['seek'])
        self.assertFalse(state['capabilities']['previous'])
        self.assertTrue(state['capabilities']['device'])
        self.assertTrue(state['devices'][0]['selected'])
        self.assertEqual(desktop.mutations, [])

    def test_other_players_are_ignored_and_apple_browser_url_is_required(self):
        client, _ = self.make(FakeDesktop({PLAYER_PREFIX + 'spotify': properties(url='spotify:track:x')}))
        self.assertFalse(client.snapshot()['available'])
        client, _ = self.make(FakeDesktop({PLAYER_PREFIX + 'firefox.instance_1': properties()}))
        self.assertTrue(client.snapshot()['available'])
        client, _ = self.make(FakeDesktop({PLAYER_PREFIX + 'chromium': properties(url='https://music.apple.com.attacker.test/x')}))
        self.assertFalse(client.snapshot()['available'])

    def test_explicit_pin_uses_only_existing_player(self):
        fake = FakeDesktop({PLAYER_PREFIX + 'custom': properties(url='')})
        client, _ = self.make(fake, preferred_player='custom')
        self.assertTrue(client.snapshot()['available'])
        client, _ = self.make(fake, preferred_player='org.freedesktop.DBus')
        self.assertFalse(client.snapshot()['available'])

    def test_playing_candidate_wins_over_paused_native(self):
        fake = FakeDesktop({PLAYER_PREFIX + 'adam': properties(PlaybackStatus='Paused'),
                            PLAYER_PREFIX + 'cider': properties(title='Other Song')})
        client, _ = self.make(fake)
        self.assertEqual(client.snapshot()['track']['title'], 'Other Song')

    def test_seek_uses_track_id_and_microseconds_not_readonly_position(self):
        client, desktop = self.make()
        self.assertTrue(client.command('seek', 42.125)['ok'])
        command = desktop.mutations[0]
        self.assertEqual(command[-3:], [PLAYER_INTERFACE + '.SetPosition', '/com/apple/music/track/42', '42125000'])

    def test_volume_is_a_typed_dbus_property_and_next_is_native_control(self):
        client, desktop = self.make()
        self.assertTrue(client.command('volume', .65)['ok'])
        self.assertEqual(desktop.mutations[-1][-4:], ['org.freedesktop.DBus.Properties.Set',
                                                    PLAYER_INTERFACE, 'Volume', '<double 0.65>'])
        self.assertTrue(client.command('next')['ok'])
        self.assertEqual(desktop.mutations[-1][-1], PLAYER_INTERFACE + '.Next')
        self.assertTrue(client.command('play_pause')['ok'])
        self.assertEqual(desktop.mutations[-1][-1], PLAYER_INTERFACE + '.PlayPause')

    def test_invalid_arguments_or_unsupported_capabilities_never_mutate(self):
        client, desktop = self.make()
        for action, value in [('seek', -1), ('seek', 181), ('seek', float('nan')),
                              ('volume', 2), ('volume', True), ('volume', 'invalid'),
                              ('previous', None), ('shell', 'rm')]:
            with self.subTest(action=action, value=value):
                self.assertFalse(client.command(action, value)['ok'])
        self.assertEqual(desktop.mutations, [])

    def test_missing_track_id_disables_seek(self):
        fake = FakeDesktop()
        fake.players[PLAYER_PREFIX + 'adam']['Metadata']['mpris:trackid'] = '/org/mpris/MediaPlayer2/TrackList/NoTrack'
        client, desktop = self.make(fake)
        self.assertFalse(client.command('seek', 1)['ok'])
        self.assertEqual(desktop.mutations, [])

    def test_device_moves_only_player_stream_and_keeps_default_sink(self):
        client, desktop = self.make()
        result = client.command('device', '20')
        self.assertTrue(result['ok'], result)
        self.assertEqual(desktop.mutations, [['/usr/bin/pactl', 'move-sink-input', '100', '20']])
        self.assertFalse(client.select_device('--help', process_id=900)['ok'])
        self.assertFalse(client.select_device('20', process_id=999)['ok'])

    def test_browser_device_routing_requires_matching_track_not_shared_pid(self):
        client, desktop = self.make()
        desktop.streams = [
            {'index': 101, 'sink': 10, 'properties': {'application.process.id': '900',
                'application.name': 'Firefox', 'media.name': 'Test Song — Apple Music'}},
            {'index': 102, 'sink': 10, 'properties': {'application.process.id': '900',
                'application.name': 'Firefox', 'media.name': 'Unrelated podcast'}}]
        result = client.select_device('20', process_id=900, player_hint='Firefox', track_title='Test Song')
        self.assertTrue(result['ok'])
        self.assertEqual(result['streams'], ['101'])
        self.assertEqual(len(desktop.mutations), 1)
        self.assertFalse(client.select_device('20', process_id=900, player_hint='Firefox')['ok'])

    def test_native_adam_engine_identity_matches_child_without_moving_cider(self):
        client, desktop = self.make()
        desktop.streams = [
            {'index': 101, 'sink': 10, 'properties': {'application.process.id': '950',
                'application.name': 'Adam', 'application.id': 'io.github.silentone12725.Adam',
                'application.process.binary': 'adam-engine'}},
            {'index': 102, 'sink': 10, 'properties': {'application.process.id': '960',
                'application.name': 'Cider'}}]
        result = client.select_device('20', process_id=900, player_hint='Adam')
        self.assertTrue(result['ok'])
        self.assertEqual(result['streams'], ['101'])

    def test_missing_tools_and_timed_out_bus_are_reported(self):
        self.assertFalse(MusicPlayback(finder=lambda _: None).snapshot()['available'])
        client, desktop = self.make()
        desktop.timeout = True
        result = client.command('next')
        self.assertFalse(result['ok'])
        self.assertIn('无法响应', result['error'])


if __name__ == '__main__':
    unittest.main()
