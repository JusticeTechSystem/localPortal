"""
EduPortal — classroom portal (attendance, CBT exams, assignments, videos, notes).

  Locally / LAN :  python app.py          -> prints the exact URL(s) to share
  Hosted        :  gunicorn app:app       -> any Python host (Render, Railway, VPS ...)

Built by JusticeTech.
"""
import argparse
import getpass
import logging
import os
import socket
import sqlite3
import sys
import time

from portal import config, db as dbm
from portal import create_app

app = create_app()


# --------------------------------------------------------------------------- #
#  Local / LAN launcher
# --------------------------------------------------------------------------- #
def get_lan_ips():
    """Best-effort list of this machine's LAN IPv4 addresses, primary first."""
    ips = []

    def add(ip):
        if ip and not ip.startswith(("127.", "169.254.", "0.")) and ip not in ips:
            ips.append(ip)

    # Connecting a UDP socket sends no packets; it only asks the OS which interface would be used.
    for target in ("8.8.8.8", "10.255.255.255", "192.168.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.3)
            s.connect((target, 1))
            add(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except OSError:
        pass
    return ips


def port_is_free(port, host="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def print_banner(host, port, server_name):
    interactive = sys.stdout.isatty()
    if interactive:
        os.system("cls" if os.name == "nt" else "clear")
    colors = ["\033[95m", "\033[94m", "\033[96m", "\033[92m", "\033[93m", "\033[91m"]
    reset, bold, dim = "\033[0m", "\033[1m", "\033[2m"
    logo = r"""
 ███████╗██████╗ ██╗   ██╗██████╗  ██████╗ ██████╗ ████████╗ █████╗ ██╗
 ██╔════╝██╔══██╗██║   ██║██╔══██╗██╔═══██╗██╔══██╗╚══██╔══╝██╔══██╗██║
 █████╗  ██║  ██║██║   ██║██████╔╝██║   ██║██████╔╝   ██║   ███████║██║
 ██╔══╝  ██║  ██║██║   ██║██╔═══╝ ██║   ██║██╔══██╗   ██║   ██╔══██║██║
 ███████╗██████╔╝╚██████╔╝██║     ╚██████╔╝██║  ██║   ██║   ██║  ██║███████╗
 ╚══════╝╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝"""
    try:
        for i, line in enumerate(logo.strip("\n").split("\n")):
            sys.stdout.write(colors[i % len(colors)] + line + reset + "\n")
            if interactive:
                time.sleep(0.05)
    except UnicodeEncodeError:
        print("EDUPORTAL")
    ok, warn = f"\033[1;92m[✓]{reset}", f"\033[1;93m[!]{reset}"
    print(f"\n{bold}\033[96m>>> {dbm.get_setting('school_name')} · EduPortal by JusticeTech <<<{reset}\n")
    print(f"{ok} Database ready  {dim}({config.DB_PATH}){reset}")
    print(f"{ok} Server: {server_name}   Timezone: {dbm.tz_label()}")

    try:
        c = sqlite3.connect(config.DB_PATH)
        default_admin = c.execute("SELECT must_change FROM staff WHERE username='admin'").fetchone()
        students = c.execute("SELECT COUNT(*) FROM authorized_students").fetchone()[0]
        c.close()
    except sqlite3.Error:
        default_admin, students = None, 0
    if default_admin and default_admin[0]:
        print(f"{warn} Admin login:  username {bold}admin{reset}  password {bold}{config.DEFAULT_ADMIN_PASSWORD}{reset}"
              f"  - you will be asked to change it at first login.")
    else:
        print(f"{ok} Staff sign in with their username + password (first admin username: {bold}admin{reset}). "
              f"Forgot it? Run: python app.py --reset-admin-password")
    if students == 0:
        print(f"{warn} No students yet. Sign in as staff, open Students, and add them or import a CSV.")
    else:
        print(f"{ok} {students} student(s) on record.")
    print()

    if host in ("127.0.0.1", "localhost"):
        print(f"{bold}Running in local-only mode (this computer only):{reset}")
        print(f"   \033[1;97mhttp://localhost:{port}{reset}\n")
        return
    ips = get_lan_ips()
    print(f"{bold}Open on THIS computer:{reset}")
    print(f"   \033[1;97mhttp://localhost:{port}{reset}\n")
    if ips:
        print(f"{bold}Share with phones/laptops on the SAME Wi-Fi / LAN cable:{reset}")
        print(f"   \033[1;92mhttp://{ips[0]}:{port}{reset}   <-- give students this link")
        for extra in ips[1:]:
            print(f"   {dim}http://{extra}:{port}   (other network adapter){reset}")
    else:
        print(f"{warn} Could not detect a LAN address. Are you connected to Wi-Fi / a network?")
    print(f"\n{dim}If other devices cannot connect, allow Python through your firewall "
          f"(see README > Troubleshooting).  Press Ctrl+C to stop.{reset}\n")


def main():
    parser = argparse.ArgumentParser(description="Run EduPortal locally / on your LAN.")
    parser.add_argument("--host", default=os.environ.get("HOST"), help="Bind address (default 0.0.0.0 = whole LAN)")
    parser.add_argument("--port", type=int, default=None, help="Port (default 5000, or the next free one)")
    parser.add_argument("--local-only", action="store_true", help="Only allow this computer (127.0.0.1)")
    parser.add_argument("--reset-admin-password", action="store_true",
                        help="Set a new password for the 'admin' account and exit")
    args = parser.parse_args()

    if args.reset_admin_password:
        pw = getpass.getpass("New admin password (min 8 characters): ")
        if len(pw) < 8 or pw != getpass.getpass("Repeat password: "):
            sys.exit("[x] Passwords must match and be at least 8 characters.")
        dbm.reset_admin_password(pw)
        print("[✓] Admin password updated. Sign in with username 'admin'.")
        return

    host = "127.0.0.1" if args.local_only else (args.host or "0.0.0.0")
    explicit_port = args.port or (int(os.environ["PORT"]) if os.environ.get("PORT") else None)
    if explicit_port:
        port = explicit_port
        if not port_is_free(port, host):
            sys.exit(f"[x] Port {port} is already in use. Pick another with --port.")
    else:
        port = next((p for p in range(5000, 5021) if port_is_free(p, host)), None)
        if port is None:
            sys.exit("[x] No free port between 5000-5020. Use --port to choose one.")

    try:
        from waitress import serve
        server_name = "Waitress (production-grade)"
    except ImportError:
        serve = None
        server_name = "Flask built-in (run `pip install -r requirements.txt` for a faster server)"

    print_banner(host, port, server_name)
    try:
        if serve:
            logging.getLogger("waitress").setLevel(logging.ERROR)
            serve(app, host=host, port=port, threads=8, channel_timeout=300,
                  max_request_body_size=(config.MAX_VIDEO_MB + 20) * 1024 * 1024)
        else:
            import flask.cli
            flask.cli.show_server_banner = lambda *a, **k: None
            app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False, load_dotenv=False)
    except KeyboardInterrupt:
        print("\nEduPortal stopped.")


if __name__ == "__main__":
    main()
