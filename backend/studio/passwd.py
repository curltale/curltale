"""Recovery from the server, for the day nobody can sign in (the only administrator forgot their password, or was locked out).

    docker compose exec app python -m studio.passwd --list              # who is there, and who can sign in
    docker compose exec app python -m studio.passwd USERNAME            # a new temporary password for USERNAME, printed once

It needs no web session: whoever can run a command in the app's container already holds the database, so this adds no new way in. It does what an
administrator's "Reset password" does (the user must choose their own password at the next sign-in, their sessions end, a sign-in lockout on the name is
lifted) and also enables the account when it was disabled. Every use is written to the audit log as `server command`.
"""
import argparse
import sys

from . import auth
from .db import J, conn


def listing():
    with conn() as c:
        rows = c.execute('SELECT username, role, disabled, must_change FROM app_user ORDER BY role DESC, username').fetchall()
    for r in rows:
        flags = ', '.join(x for x, on in (('disabled', r['disabled']), ('must change password', r['must_change'])) if on)
        print(f"{r['username']:<24} {r['role']:<10} {flags}")
    return 0


def recover(username):
    with conn() as c:
        u = c.execute('SELECT username, disabled FROM app_user WHERE username=%s', (username,)).fetchone()
    if not u:
        print(f'no user named {username!r}. List the users with: python -m studio.passwd --list', file=sys.stderr)
        return 1
    password = auth.reset_password(username)
    with conn() as c:
        if u['disabled']:
            c.execute('UPDATE app_user SET disabled=false WHERE username=%s', (username,))
        c.execute("INSERT INTO audit_log (username,action,kind,key,after) VALUES ('server command','password_reset','user',%s,%s)",
                  (f'user:{username}', J({'generated': True, 'enabled_again': bool(u['disabled'])})))
    print(f'New temporary password for {username}: {password}')
    print('Shown once. Sign in with it: you will be asked to choose your own password straight away.')
    if u['disabled']:
        print('The account was disabled: it is enabled again.')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m studio.passwd', description='Give a user a new temporary password from the server, or list the users.')
    ap.add_argument('username', nargs='?', help='the user to recover')
    ap.add_argument('--list', action='store_true', help='list the users')
    args = ap.parse_args(argv)
    if args.list:
        return listing()
    if not args.username:
        ap.print_usage(sys.stderr)
        return 2
    return recover(args.username)


if __name__ == '__main__':
    sys.exit(main())
