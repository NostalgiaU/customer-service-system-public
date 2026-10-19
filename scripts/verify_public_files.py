"""Fail closed if a tracked path is outside the reviewed public release.

This checks paths, not file contents. Run a secret scanner as well.
"""
from pathlib import Path
import subprocess
import sys


ALLOWED = {
    '.env.example', '.gitignore', '.dockerignore',
    '.github/workflows/checks.yml',
    'README.md', 'LICENSE', 'SECURITY.md', 'requirements.txt',
    'config.py', 'conversation.py', 'crypto.py', 'delivery.py',
    'delivery_service.py', 'main.py', 'media.py', 'store.py',
    'weiyu.py', 'worker.py', 'wxstore.py',
    'scripts/verify_public_files.py',
    'tests/test_conversation_delivery.py', 'tests/test_conversations.py',
    'tests/test_media_delivery.py',
}


def main():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root)
    paths = {p.decode('utf-8') for p in tracked.split(b'\0') if p}
    rejected = sorted(paths - ALLOWED)
    if rejected:
        print('Refusing unreviewed tracked files:', *rejected, sep='\n', file=sys.stderr)
        return 1
    if not paths:
        print('No tracked files to review.', file=sys.stderr)
        return 1
    print(f'Public file allowlist passed: {len(paths)} tracked files.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
