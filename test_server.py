import json
import hashlib
import os
import tempfile
import unittest
from unittest import mock

import server


class MusicTrimTests(unittest.TestCase):
    def test_music_settings_survive_unrelated_project_save(self):
        with tempfile.TemporaryDirectory() as folder:
            selections = {'clip.mp4': {'filename': 'clip.mp4', 'enabled': True}}
            server.save_selections(
                folder,
                selections,
                music_ends={'song.mp3': 12.0},
                music_offsets={'song.mp3': 1.0},
                music_starts={'song.mp3': 4.0},
                title2='second line',
            )

            server.save_selections(folder, selections, title='updated')

            with open(os.path.join(folder, 'selections.json'), encoding='utf-8') as saved_file:
                saved = json.load(saved_file)
            self.assertEqual(saved['music_ends'], {'song.mp3': 12.0})
            self.assertEqual(saved['music_offsets'], {'song.mp3': 1.0})
            self.assertEqual(saved['music_trim_starts'], {'song.mp3': 4.0})
            self.assertEqual(saved['title2'], 'second line')

            loaded = server.load_selections(folder)
            self.assertEqual(loaded[10], {'song.mp3': 4.0})

    def test_times_snap_to_output_frames(self):
        self.assertEqual(server.snap_to_output_frame(1.01), 1.0)
        self.assertEqual(server.snap_to_output_frame(1.02), 31 / 30)

    @mock.patch.object(server, 'run_cmd')
    @mock.patch.object(server, 'ffprobe_info', return_value=(20.0, 1080, 1920, 'h264', False))
    def test_music_mix_trims_from_source_start(self, _probe, run_cmd):
        run_cmd.return_value = (0, b'', b'')
        tracks = [{
            'filename': 'song.mp3',
            'duration': 12.0,
            'track_start': 2.0,
            'track_end': 8.0,
            'track_offset': 1.0,
        }]

        result = server._add_music_to_video('project', 'video.mp4', 'out.mp4', tracks)

        self.assertEqual(result, 'out.mp4')
        command = run_cmd.call_args.args[0]
        filter_graph = command[command.index('-filter_complex') + 1]
        self.assertIn('atrim=2.000000:8.000000,asetpts=PTS-STARTPTS', filter_graph)
        self.assertIn('afade=t=in:st=0:d=3.000000', filter_graph)
        self.assertIn('afade=t=out:st=3.000000:d=3.000000', filter_graph)
        self.assertIn('adelay=delays=44100S:all=1', filter_graph)
        self.assertIn('afade=t=out:st=10.000:d=10.000[mus]', filter_graph)
        self.assertEqual(filter_graph.count('asetpts=PTS-STARTPTS'), 1)

    @mock.patch.object(server, 'run_cmd')
    @mock.patch.object(server, 'ffprobe_info', return_value=(30.0, 1080, 1920, 'h264', False))
    def test_multiple_tracks_use_absolute_sample_aligned_positions(self, _probe, run_cmd):
        run_cmd.return_value = (0, b'', b'')
        tracks = [
            {'filename': 'one.mp3', 'duration': 10.0, 'track_end': 5.0,
             'track_start': 2.0, 'track_offset': 1 / 30},
            {'filename': 'two.mp3', 'duration': 10.0, 'track_end': 4.0,
             'track_start': 1.0, 'track_offset': 2 / 30},
        ]

        server._add_music_to_video('project', 'video.mp4', 'out.mp4', tracks)

        command = run_cmd.call_args.args[0]
        filter_graph = command[command.index('-filter_complex') + 1]
        self.assertIn('adelay=delays=1470S:all=1', filter_graph)
        self.assertIn('adelay=delays=136710S:all=1', filter_graph)
        self.assertIn('amix=inputs=2:duration=longest:dropout_transition=0:normalize=0', filter_graph)
        self.assertNotIn('concat=n=2', filter_graph)
        self.assertNotIn('[mus_raw]atrim', filter_graph)

    @mock.patch.object(server, '_run_ffmpeg_progress', return_value=(0, b'', b''))
    @mock.patch.object(server, 'ffprobe_info', return_value=(20.0, 1080, 1920, 'h264', False))
    def test_music_mix_enables_ffmpeg_progress_reporting(self, _probe, run_progress):
        callback = mock.Mock()
        tracks = [{'filename': 'song.mp3', 'duration': 10.0}]

        result = server._add_music_to_video(
            'project', 'video.mp4', 'out.mp4', tracks, progress_callback=callback
        )

        self.assertEqual(result, 'out.mp4')
        command = run_progress.call_args.args[0]
        self.assertIn('-progress', command)
        self.assertEqual(command[command.index('-progress') + 1], 'pipe:1')
        self.assertIn('-nostats', command)
        self.assertEqual(run_progress.call_args.args[1:3], (20.0, 'music mix'))

    def test_export_without_music_reaches_done_after_cached_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, '.clip_cache'))
            clip = {'filename': 'clip.mp4', 'modified': '2026-08-30T12:00:00'}
            selections = {'clip.mp4': {'filename': 'clip.mp4', 'enabled': True, 'start_time': 0}}
            clip_cache = server._clip_cache_path(folder, 'clip.mp4', 0, 3.0)
            end_cache = server._end_card_cache_path(folder, '', '')
            for path in (clip_cache, end_cache):
                with open(path, 'wb') as cached_file:
                    cached_file.write(b'cached')

            concat_key = '|'.join(os.path.abspath(path).replace('\\', '/')
                                  for path in (clip_cache, end_cache))
            concat_hash = hashlib.md5(concat_key.encode()).hexdigest()[:16]
            concat_cache = os.path.join(folder, '.clip_cache', f'concat_{concat_hash}.mp4')
            with open(concat_cache, 'wb') as cached_file:
                cached_file.write(b'merged')

            server.export_cancel = False
            server.export_worker(
                folder, [clip], selections, 'final.mp4', music_tracks=[], include_day_cards=False
            )

            self.assertEqual(server.export_status['status'], 'done')
            self.assertEqual(server.export_status['percent'], 100)
            self.assertTrue(os.path.isfile(os.path.join(folder, 'output', 'final.mp4')))
            self.assertTrue(os.path.isfile(concat_cache))


class TitleCardTests(unittest.TestCase):
    def test_drawtext_preserves_apostrophe_as_typographic_quote(self):
        self.assertEqual(server._sq("Pachol'a"), 'Pachol\u2019a')

    def test_long_title_wraps_without_losing_words(self):
        title = 'A very long holiday title that must fit inside the exported portrait frame'

        lines = server._wrap_card_text(title)

        self.assertGreater(len(lines), 1)
        self.assertEqual(' '.join(lines), title)
        self.assertTrue(all(server._card_text_width(line) <= 19.0 for line in lines))

    @mock.patch.object(server, 'run_cmd', return_value=(0, b'', b''))
    @mock.patch.object(server, 'find_icon_font', return_value='C:/Windows/Fonts/seguisym.ttf')
    @mock.patch.object(server, 'find_text_font', return_value='C:/Windows/Fonts/segoeui.ttf')
    def test_title_card_draws_each_wrapped_line(self, _text_font, _icon_font, run_cmd):
        title = 'A very long holiday title that must wrap in the exported film'
        expected_lines = server._wrap_card_text(title)

        result = server.generate_title_card(title, 'Summer 2026', 'title.mp4')

        self.assertEqual(result, 'title.mp4')
        video_filter = run_cmd.call_args_list[0].args[0]
        video_filter = video_filter[video_filter.index('-vf') + 1]
        for line in expected_lines:
            self.assertIn(f"text='{line}'", video_filter)
        self.assertNotIn(f"text='{title}'", video_filter)

    @mock.patch.object(server, 'run_cmd', return_value=(0, b'', b''))
    @mock.patch.object(server, 'find_text_font', return_value='C:/Windows/Fonts/segoeui.ttf')
    def test_day_card_draws_wrapped_custom_title(self, _text_font, run_cmd):
        title = 'A long custom day title that must wrap in the exported film'
        expected_lines = server._wrap_card_text(title)

        result = server.generate_day_card(
            '2026-08-30', 'Sunday', 'day.mp4', title_override=title
        )

        self.assertEqual(result, 'day.mp4')
        command = run_cmd.call_args.args[0]
        video_filter = command[command.index('-vf') + 1]
        for line in expected_lines:
            self.assertIn(f"text='{line}'", video_filter)
        self.assertNotIn(f"text='{title}'", video_filter)

    @mock.patch.object(server, 'run_cmd', return_value=(0, b'', b''))
    @mock.patch.object(server, 'find_text_font', return_value='C:/Windows/Fonts/segoeui.ttf')
    def test_day_card_with_apostrophe_keeps_second_line(self, _text_font, run_cmd):
        title = "Brestova - Salatin - Pachol'a"

        server.generate_day_card('2026-08-30', 'Sunday', 'day.mp4', title_override=title)

        command = run_cmd.call_args.args[0]
        video_filter = command[command.index('-vf') + 1]
        self.assertIn("text='Brestova - Salatin -'", video_filter)
        self.assertIn("text='Pachol\u2019a'", video_filter)


if __name__ == '__main__':
    unittest.main()
