"""Isolated ffsubsync adapter with machine-readable success and diagnostics."""
import json
import sys


def main():
    from ffsubsync.ffsubsync import make_parser, run
    config = json.loads(sys.stdin.readline())
    args = [config['reference'], '-i', config['subtitle'], '-o', config['output'],
            '--encoding', 'utf-8', '--output-encoding', 'utf-8', '--skip-sync-on-low-quality']
    if config.get('audio_stream') is not None:
        args += ['--reference-stream', f"0:{config['audio_stream']}", '--vad', 'webrtc']
    if config.get('split'):
        args += ['--split-penalty', '10']
    result = run(make_parser().parse_args(args))
    print(json.dumps({'success': bool(result.get('sync_was_successful')),
                      'offset_seconds': result.get('offset_seconds'),
                      'scale': result.get('framerate_scale_factor')}), flush=True)


if __name__ == '__main__':
    main()
