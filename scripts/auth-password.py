#!/usr/bin/env python3
"""Set Compose credentials without exposing the password on the command line."""
import getpass
import re
import subprocess
from pathlib import Path

path = Path('.env')
user = input('Username [admin]: ').strip() or 'admin'
if not re.fullmatch(r'[A-Za-z0-9_.-]+', user):
    raise SystemExit('Username may contain only letters, digits, dot, dash and underscore.')
password = getpass.getpass('Password: ')
if not password or password != getpass.getpass('Confirm password: '):
    raise SystemExit('Password is empty or confirmation does not match.')
result = subprocess.run(['uv', 'run', '--quiet', '--with', 'bcrypt', 'python', '-c',
                         'import bcrypt,sys; print(bcrypt.hashpw(sys.stdin.buffer.read(), bcrypt.gensalt()).decode())'],
                        input=password.encode(), stdout=subprocess.PIPE, check=True)
hash_value = result.stdout.decode().strip()
text = path.read_text()
for key, value in [('AUTH_USER', user), ('AUTH_HASH', "'" + hash_value + "'")]:
    text = re.sub(rf'^\s*{key}=.*$', '', text, flags=re.MULTILINE)
    text += f'\n{key}={value}\n'
path.write_text(text)
print('Credentials saved. Run make proxy to apply them.')
