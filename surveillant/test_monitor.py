#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests automatiques du système de surveillance. Lancer : python3 -m unittest -v test_monitor"""

import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import monitor
import rapport

PARAMS = {**monitor.PARAMETRES_PAR_DEFAUT, "pause_nouvel_essai_s": 0, "timeout_s": 3}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _repondre(self, code, corps=b"", entetes=None):
        self.send_response(code)
        for k, v in (entetes or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        port = self.server.server_port
        if self.path == "/ok":
            self._repondre(200, "<html>Bienvenue sur Agir pour ma vie</html>".encode("utf-8"))
        elif self.path == "/vide":
            self._repondre(200, b"<html>rien du tout</html>")
        elif self.path == "/500":
            self._repondre(500, b"erreur")
        elif self.path == "/403":
            self._repondre(403, b"interdit")
        elif self.path == "/defi":
            self._repondre(403, b"Just a moment...", {"cf-mitigated": "challenge", "Server": "cloudflare"})
        elif self.path == "/redir":
            self._repondre(302, b"", {"Location": "/ok"})
        elif self.path == "/vers-autre":
            self._repondre(302, b"", {"Location": f"http://127.0.0.1:{port}/ok"})
        elif self.path == "/boucle":
            self._repondre(302, b"", {"Location": "/boucle"})
        elif self.path == "/lent":
            time.sleep(3)
            try:
                self._repondre(200, b"tard")
            except (BrokenPipeError, ConnectionResetError):
                pass  # le contrôleur a déjà abandonné : c'est justement ce que le test vérifie
        else:
            self._repondre(404, b"introuvable")


def demarrer(handler=Handler, contexte_ssl=None):
    serveur = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if contexte_ssl:
        serveur.socket = contexte_ssl.wrap_socket(serveur.socket, server_side=True)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    return serveur


class TestControleHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.serveur = demarrer()
        cls.base = f"http://127.0.0.1:{cls.serveur.server_port}"
        cls.base_localhost = f"http://localhost:{cls.serveur.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.serveur.shutdown()

    def controler(self, chemin, base=None, **champs):
        site = {"id": "t", "url": (base or self.base) + chemin, **champs}
        return monitor.controler_site(site, PARAMS, pause=lambda s: None)

    def test_page_normale(self):
        r = self.controler("/ok")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["statut"], 200)
        self.assertIsNotNone(r["ms"])

    def test_erreur_500(self):
        r = self.controler("/500")
        self.assertFalse(r["ok"])
        self.assertEqual(r["cause"], "http_5xx")

    def test_page_introuvable(self):
        self.assertEqual(self.controler("/nimporte")["cause"], "http_404")

    def test_acces_refuse(self):
        self.assertEqual(self.controler("/403")["cause"], "http_4xx")

    def test_verification_anti_robots_n_est_pas_une_panne(self):
        r = self.controler("/defi", mot_cle="Agir pour ma vie")
        self.assertTrue(r["ok"], r)
        self.assertIn("anti-robots", r["note"])

    def test_redirection_suivie(self):
        r = self.controler("/redir")
        self.assertTrue(r["ok"])
        self.assertTrue(r["url_finale"].endswith("/ok"))

    def test_boucle_de_redirections(self):
        self.assertEqual(self.controler("/boucle")["cause"], "boucle_redirection")

    def test_mot_cle_present_sans_tenir_compte_des_majuscules(self):
        self.assertTrue(self.controler("/ok", mot_cle="AGIR POUR MA VIE")["ok"])

    def test_mot_cle_absent(self):
        r = self.controler("/vide", mot_cle="Agir pour ma vie")
        self.assertEqual(r["cause"], "contenu")
        self.assertIn("Agir pour ma vie", r["detail"])

    def test_plusieurs_mots_cles(self):
        self.assertTrue(self.controler("/ok", mot_cle=["Bienvenue", "Agir"])["ok"])
        self.assertEqual(self.controler("/ok", mot_cle=["Bienvenue", "Absent"])["cause"], "contenu")

    def test_redirection_vers_la_bonne_adresse(self):
        r = self.controler("/redir", base=self.base_localhost,
                           redirige_vers=self.base_localhost + "/ok")
        self.assertTrue(r["ok"], r)

    def test_redirection_vers_la_mauvaise_adresse(self):
        r = self.controler("/vers-autre", base=self.base_localhost,
                           redirige_vers=self.base_localhost + "/ok")
        self.assertEqual(r["cause"], "redirection")

    def test_pas_de_redirection_alors_qu_on_en_attend_une(self):
        r = self.controler("/ok", redirige_vers="http://exemple.test/")
        self.assertEqual(r["cause"], "redirection")

    def test_delai_depasse(self):
        params = {**PARAMS, "timeout_s": 1}
        site = {"id": "t", "url": self.base + "/lent"}
        r = monitor.controler_site(site, params, pause=lambda s: None)
        self.assertEqual(r["cause"], "timeout")

    def test_connexion_refusee(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = monitor.controler_site({"id": "t", "url": f"http://127.0.0.1:{port}/"}, PARAMS, pause=lambda s: None)
        self.assertEqual(r["cause"], "connexion")

    def test_second_essai_rattrape_un_incident_passager(self):
        class Capricieux(Handler):
            appels = 0

            def do_GET(self):
                Capricieux.appels += 1
                if Capricieux.appels == 1:
                    self._repondre(500, b"x")
                else:
                    self._repondre(200, b"ok")

        serveur = demarrer(Capricieux)
        try:
            site = {"id": "t", "url": f"http://127.0.0.1:{serveur.server_port}/"}
            r = monitor.controler_site(site, PARAMS, pause=lambda s: None)
            self.assertTrue(r["ok"])
            self.assertEqual(Capricieux.appels, 2)
        finally:
            serveur.shutdown()

    def test_classement_des_erreurs(self):
        self.assertEqual(monitor.classer_exception(socket.gaierror(-2, "inconnu"))[0], "dns")
        self.assertEqual(monitor.classer_exception(socket.timeout())[0], "timeout")
        self.assertEqual(monitor.classer_exception(TimeoutError())[0], "timeout")
        self.assertEqual(monitor.classer_exception(ConnectionRefusedError())[0], "connexion")
        self.assertEqual(monitor.classer_exception(ssl.SSLError("x"))[0], "ssl")
        self.assertEqual(monitor.classer_exception(ValueError("x"))[0], "inconnue")


@unittest.skipUnless(shutil.which("openssl"), "openssl absent")
class TestCertificat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dossier = tempfile.mkdtemp()
        cert = Path(cls.dossier) / "c.pem"
        cle = Path(cls.dossier) / "k.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(cle), "-out", str(cert),
             "-days", "5", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, capture_output=True)
        serveur_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        serveur_ctx.load_cert_chain(str(cert), str(cle))
        cls.serveur = demarrer(contexte_ssl=serveur_ctx)
        cls.url = f"https://127.0.0.1:{cls.serveur.server_port}/ok"
        cls.cert = str(cert)

    @classmethod
    def tearDownClass(cls):
        cls.serveur.shutdown()
        shutil.rmtree(cls.dossier, ignore_errors=True)

    def test_jours_restants_du_certificat(self):
        ctx = ssl.create_default_context(cafile=self.cert)
        r = monitor.controler_site({"id": "t", "url": self.url}, PARAMS, ssl_context=ctx, pause=lambda s: None)
        self.assertTrue(r["ok"], r)
        self.assertTrue(4.0 < r["ssl_jours"] <= 5.0, r["ssl_jours"])

    def test_certificat_non_reconnu(self):
        r = monitor.controler_site({"id": "t", "url": self.url}, PARAMS, pause=lambda s: None)
        self.assertEqual(r["cause"], "ssl_invalide", r)


def faux_envoyeur(journal, succes=True):
    def envoyer(genre, site, res, e, maintenant):
        journal.append((genre, site["id"]))
        return succes
    return envoyer


SITE = {"id": "s1", "nom": "Site test", "url": "https://exemple.test", "entreprise": "APMvie",
        "critique": True, "hebergeur": "OVH", "reparation": "claude"}
OK = {"id": "s1", "ok": True, "cause": None, "detail": "", "ms": 120, "ssl_jours": None}
KO = {"id": "s1", "ok": False, "cause": "timeout", "detail": "aucune réponse", "ms": None, "ssl_jours": None}
T0 = monitor.parse_iso("2026-10-12T12:00:00Z")


def minute(n):
    return T0 + timedelta(minutes=n)


class TestMachineEtats(unittest.TestCase):
    def passer(self, etat, res, quand, envoyeur, site=SITE):
        return monitor.appliquer([site], {site["id"]: res}, etat, PARAMS, envoyeur, quand)

    def test_un_echec_isole_ne_declenche_rien(self):
        journal, etat = [], {"sites": {}}
        self.passer(etat, KO, minute(0), faux_envoyeur(journal))
        self.passer(etat, OK, minute(5), faux_envoyeur(journal))
        self.assertEqual(journal, [])
        self.assertEqual(etat["sites"], {})  # l'état revient à vide

    def test_alerte_apres_deux_echecs_et_une_seule_fois(self):
        journal, etat = [], {"sites": {}}
        env = faux_envoyeur(journal)
        self.passer(etat, KO, minute(0), env)
        self.assertEqual(journal, [])
        self.passer(etat, KO, minute(5), env)
        self.assertEqual(journal, [("panne", "s1")])
        for i in (10, 15, 20, 25):
            self.passer(etat, KO, minute(i), env)
        self.assertEqual(journal, [("panne", "s1")])

    def test_l_etat_ne_change_plus_pendant_une_panne(self):
        journal, etat = [], {"sites": {}}
        env = faux_envoyeur(journal)
        for i in (0, 5, 10):
            self.passer(etat, KO, minute(i), env)
        avant = json.dumps(etat, sort_keys=True)
        self.passer(etat, KO, minute(15), env)
        self.assertEqual(avant, json.dumps(etat, sort_keys=True))

    def test_rappel_apres_une_heure_puis_toutes_les_6_heures(self):
        journal, etat = [], {"sites": {}}
        env = faux_envoyeur(journal)
        self.passer(etat, KO, minute(0), env)
        self.passer(etat, KO, minute(5), env)
        self.passer(etat, KO, minute(30), env)
        self.assertEqual(journal, [("panne", "s1")])
        self.passer(etat, KO, minute(61), env)
        self.assertEqual(journal[-1], ("rappel", "s1"))
        self.passer(etat, KO, minute(120), env)
        self.assertEqual(len(journal), 2)
        self.passer(etat, KO, minute(61 + 6 * 60), env)
        self.assertEqual(journal[-1], ("rappel", "s1"))
        self.assertEqual(len(journal), 3)

    def test_retour_a_la_normale_enregistre_la_panne(self):
        journal, etat = [], {"sites": {}}
        env = faux_envoyeur(journal)
        self.passer(etat, KO, minute(0), env)
        self.passer(etat, KO, minute(5), env)
        incidents = self.passer(etat, OK, minute(20), env)
        self.assertEqual(journal, [("panne", "s1"), ("retour", "s1")])
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["duree_s"], 20 * 60)
        self.assertEqual(incidents[0]["cause"], "timeout")
        self.assertEqual(etat["sites"], {})

    def test_alerte_non_envoyee_est_retentee_au_controle_suivant(self):
        journal, etat = [], {"sites": {}}
        self.passer(etat, KO, minute(0), faux_envoyeur(journal, succes=False))
        self.passer(etat, KO, minute(5), faux_envoyeur(journal, succes=False))
        self.assertFalse(etat["sites"]["s1"].get("alerte_envoyee"))
        self.passer(etat, KO, minute(10), faux_envoyeur(journal, succes=True))
        self.assertEqual([g for g, _ in journal], ["panne", "panne"])
        self.assertTrue(etat["sites"]["s1"]["alerte_envoyee"])

    def test_pas_de_message_retour_si_l_alerte_n_est_jamais_partie(self):
        journal, etat = [], {"sites": {}}
        env_ko = faux_envoyeur(journal, succes=False)
        self.passer(etat, KO, minute(0), env_ko)
        self.passer(etat, KO, minute(5), env_ko)
        incidents = self.passer(etat, OK, minute(10), env_ko)
        self.assertEqual([g for g, _ in journal], ["panne"])
        self.assertEqual(len(incidents), 1)  # la panne est quand même enregistrée

    def test_seuil_specifique_a_un_site(self):
        site = {**SITE, "seuil_echecs": 3}
        journal, etat = [], {"sites": {}}
        env = faux_envoyeur(journal)
        self.passer(etat, KO, minute(0), env, site)
        self.passer(etat, KO, minute(5), env, site)
        self.assertEqual(journal, [])
        self.passer(etat, KO, minute(10), env, site)
        self.assertEqual(journal, [("panne", "s1")])

    def test_alertes_certificat_14_7_3_jours(self):
        journal, etat = [], {"sites": {}}
        env = faux_envoyeur(journal)
        def avec_jours(j):
            return {**OK, "ssl_jours": j}
        self.passer(etat, avec_jours(30), minute(0), env)
        self.assertEqual(journal, [])
        self.passer(etat, avec_jours(13.5), minute(5), env)
        self.passer(etat, avec_jours(13.0), minute(10), env)
        self.assertEqual(journal, [("ssl", "s1")])
        self.passer(etat, avec_jours(6.9), minute(15), env)
        self.passer(etat, avec_jours(2.5), minute(20), env)
        self.assertEqual([g for g, _ in journal], ["ssl", "ssl", "ssl"])
        self.passer(etat, avec_jours(2.4), minute(25), env)
        self.assertEqual(len(journal), 3)
        self.passer(etat, avec_jours(89), minute(30), env)  # certificat renouvelé
        self.assertNotIn("ssl_niveau", etat["sites"].get("s1", {}))
        self.passer(etat, avec_jours(10), minute(35), env)
        self.assertEqual(len(journal), 4)

    def test_site_desactive_disparait_de_l_etat(self):
        etat = {"sites": {"vieux": {"echecs": 2, "premier_echec": "2026-10-12T10:00:00Z", "alerte_envoyee": True}}}
        self.passer(etat, OK, minute(0), faux_envoyeur([]))
        self.assertNotIn("vieux", etat["sites"])


class TestMessages(unittest.TestCase):
    def setUp(self):
        self.e = {"echecs": 2, "premier_echec": "2026-10-12T12:00:00Z", "cause": "timeout", "detail": "aucune réponse"}

    def test_panne_contient_l_essentiel(self):
        m = monitor.construire_message("panne", SITE, KO, self.e, minute(10), PARAMS)
        self.assertIn("Site test", m["sujet"])
        self.assertIn("https://exemple.test", m["corps"])
        self.assertIn("Message à copier-coller à Claude", m["corps"])
        self.assertIn("14:00", m["corps"])  # 12:00 UTC = 14:00 à Paris (heure d'été)
        self.assertEqual(m["priorite"], 5)

    def test_sms_court_et_sans_accents(self):
        m = monitor.construire_message("panne", SITE, KO, self.e, minute(10), PARAMS)
        self.assertTrue(m["sms"].isascii())
        self.assertLessEqual(len(m["sms"]), 200)

    def test_retour_indique_la_duree(self):
        m = monitor.construire_message("retour", SITE, OK, self.e, minute(20), PARAMS)
        self.assertIn("20 min", m["corps"])

    def test_plateforme_tierce_explique_la_limite(self):
        site = {**SITE, "reparation": "plateforme", "hebergeur": "Systeme.io"}
        m = monitor.construire_message("panne", site, KO, self.e, minute(10), PARAMS)
        self.assertIn("plateforme tierce (Systeme.io)", m["corps"])

    def test_tous_les_genres_sont_construits(self):
        for genre in ("panne", "rappel", "retour", "ssl", "test"):
            m = monitor.construire_message(genre, SITE, KO, self.e, minute(90), PARAMS, jours_ssl=5)
            for cle in ("sujet", "corps", "push_titre", "push_texte", "priorite", "tags"):
                self.assertIn(cle, m)

    def test_toutes_les_causes_ont_un_texte(self):
        for cle, (libelle, piste) in monitor.CAUSES.items():
            self.assertTrue(libelle and piste, cle)

    def test_email_en_utf8(self):
        env = {"SMTP_USER": "moi@exemple.test", "SMTP_PASSWORD": "x", "ALERT_EMAIL_TO": "toi@exemple.test"}
        m = monitor.construire_message("panne", SITE, KO, self.e, minute(10), PARAMS)
        mail = monitor.construire_email(env, m["sujet"], m["corps"])
        self.assertEqual(mail["To"], "toi@exemple.test")
        self.assertIn("🔴", str(mail["Subject"]))
        self.assertIn("é", mail.get_content())


class TestCanaux(unittest.TestCase):
    def test_canaux_detectes(self):
        self.assertEqual(monitor.Notifier(env={}).canaux(), [])
        n = monitor.Notifier(env={"SMTP_USER": "a", "SMTP_PASSWORD": "b", "NTFY_TOPIC": "t",
                                  "FREE_SMS_USER": "u", "FREE_SMS_KEY": "k"})
        self.assertEqual(n.canaux(), ["e-mail", "push", "sms"])

    def test_aucun_canal_donne_un_echec(self):
        m = monitor.construire_message("test", SITE, None, {}, minute(0), PARAMS)
        self.assertFalse(monitor.Notifier(env={}).envoyer(m))

    def test_url_sms(self):
        url = monitor.url_sms({"FREE_SMS_USER": "123", "FREE_SMS_KEY": "abc"}, "Bonjour é")
        self.assertTrue(url.startswith("https://smsapi.free-mobile.fr/sendmsg?user=123&pass=abc&msg="))
        self.assertIn("Bonjour+%C3%A9", url)

    def test_push_ntfy_envoye_en_json(self):
        recu = {}

        class Capteur(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                taille = int(self.headers["Content-Length"])
                recu["chemin"] = self.path
                recu["corps"] = json.loads(self.rfile.read(taille))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        serveur = demarrer(Capteur)
        try:
            env = {"NTFY_TOPIC": "sujet-secret", "NTFY_SERVER": f"http://127.0.0.1:{serveur.server_port}"}
            m = monitor.construire_message("panne", SITE, KO,
                                           {"premier_echec": "2026-10-12T12:00:00Z", "cause": "timeout"}, minute(10), PARAMS)
            self.assertTrue(monitor.Notifier(env=env).envoyer(m))
        finally:
            serveur.shutdown()
        self.assertEqual(recu["chemin"], "/")
        self.assertEqual(recu["corps"]["topic"], "sujet-secret")
        self.assertEqual(recu["corps"]["priority"], 5)
        self.assertIn("rotating_light", recu["corps"]["tags"])
        self.assertIn("Site test", recu["corps"]["title"])


class TestConfiguration(unittest.TestCase):
    def ecrire(self, contenu):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        f.write(contenu)
        f.close()
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        return f.name

    def test_le_vrai_sites_json_est_valide(self):
        config = monitor.charger_config(monitor.DOSSIER / "sites.json")
        self.assertGreaterEqual(len(config["sites"]), 1)

    def test_doublon_refuse(self):
        with self.assertRaises(SystemExit):
            monitor.charger_config(self.ecrire('{"sites":[{"id":"a","url":"https://x.fr"},{"id":"a","url":"https://y.fr"}]}'))

    def test_adresse_sans_http_refusee(self):
        with self.assertRaises(SystemExit):
            monitor.charger_config(self.ecrire('{"sites":[{"id":"a","url":"x.fr"}]}'))

    def test_json_casse_donne_un_message_clair(self):
        with self.assertRaises(SystemExit) as ctx:
            monitor.charger_config(self.ecrire('{"sites": [ {"id": "a" "url": "https://x.fr"} ]}'))
        self.assertIn("mal écrit", str(ctx.exception))


class TestRapport(unittest.TestCase):
    def test_disponibilite_sur_sept_jours(self):
        config = {"sites": [{"id": "s1", "nom": "Site test", "url": "https://exemple.test", "actif": True},
                            {"id": "s2", "nom": "Plus tard", "url": "https://plus-tard.test", "actif": False}]}
        maintenant = monitor.parse_iso("2026-10-12T06:00:00Z")
        incidents = [
            {"site": "s1", "debut": "2026-10-10T10:00:00Z", "fin": "2026-10-10T11:10:00Z", "cause": "http_5xx"},
            {"site": "s1", "debut": "2026-09-01T10:00:00Z", "fin": "2026-09-01T12:00:00Z", "cause": "dns"},  # hors fenêtre
        ]
        resultats = {"s1": {"ok": True, "ssl_jours": 61.0}}
        texte = rapport.construire_rapport(config, PARAMS, {"sites": {}}, incidents, resultats, maintenant)
        self.assertIn("99.31 %", texte)  # 70 min sur 10 080 min
        self.assertIn("1 panne(s)", texte)
        self.assertIn("Plus tard", texte)  # listé comme non surveillé
        self.assertIn("61 j", texte)

    def test_panne_en_cours_comptee(self):
        config = {"sites": [{"id": "s1", "nom": "Site test", "url": "https://exemple.test"}]}
        maintenant = monitor.parse_iso("2026-10-12T06:00:00Z")
        etat = {"sites": {"s1": {"echecs": 2, "premier_echec": "2026-10-12T05:00:00Z", "cause": "timeout",
                                 "alerte_envoyee": True}}}
        texte = rapport.construire_rapport(config, PARAMS, etat, [], {"s1": {"ok": False, "cause": "timeout", "ssl_jours": None}}, maintenant)
        self.assertIn("panne en cours", texte)
        self.assertIn("PROBLÈME", texte)

    def test_aucun_site_actif(self):
        config = {"sites": [{"id": "s2", "nom": "Plus tard", "url": "https://plus-tard.test", "actif": False}]}
        texte = rapport.construire_rapport(config, PARAMS, {"sites": {}}, [], {}, monitor.maintenant_utc())
        self.assertIn("Aucun site n'est actif", texte)


class TestDeBoutEnBout(unittest.TestCase):
    """Fait tourner le vrai programme plusieurs fois contre un faux site qu'on casse puis qu'on répare."""

    def test_panne_puis_retour(self):
        recus = []

        class Capteur(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                recus.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        etat_site = {"code": 200}

        class Site(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(etat_site["code"])
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        ntfy, site = demarrer(Capteur), demarrer(Site)
        dossier = Path(tempfile.mkdtemp())
        try:
            (dossier / "sites.json").write_text(json.dumps({
                "parametres": {"pause_nouvel_essai_s": 0, "timeout_s": 3},
                "sites": [
                    {"id": "demo", "nom": "Site de démonstration", "entreprise": "Démo", "critique": True,
                     "url": f"http://127.0.0.1:{site.server_port}/", "actif": True},
                    {"id": "demo-inactif", "url": "http://127.0.0.1:9/", "actif": False},
                ]}), encoding="utf-8")
            env = {"NTFY_TOPIC": "demo", "NTFY_SERVER": f"http://127.0.0.1:{ntfy.server_port}"}
            args = ["--config", str(dossier / "sites.json"), "--etat", str(dossier / "state.json"),
                    "--incidents", str(dossier / "incidents.jsonl")]

            self.assertEqual(monitor.main(args, env=env), 0)           # tout va bien
            self.assertEqual(recus, [])

            etat_site["code"] = 500
            self.assertEqual(monitor.main(args, env=env), 0)           # 1er échec : silence
            self.assertEqual(recus, [])
            self.assertEqual(monitor.main(args, env=env), 0)           # 2e échec : alerte
            self.assertEqual(len(recus), 1)
            self.assertIn("en panne", recus[0]["title"])
            self.assertEqual(monitor.main(args, env=env), 0)           # toujours en panne : pas de doublon
            self.assertEqual(len(recus), 1)

            etat_site["code"] = 200
            self.assertEqual(monitor.main(args, env=env), 0)           # retour
            self.assertEqual(len(recus), 2)
            self.assertIn("Rétabli", recus[1]["title"])

            lignes = (dossier / "incidents.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lignes), 1)
            self.assertEqual(json.loads(lignes[0])["cause"], "http_5xx")
            self.assertEqual(json.loads((dossier / "state.json").read_text(encoding="utf-8")), {"sites": {}})
        finally:
            ntfy.shutdown()
            site.shutdown()
            shutil.rmtree(dossier, ignore_errors=True)

    def test_aucun_canal_configure_fait_echouer_le_controle(self):
        class Casse(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

        site = demarrer(Casse)
        dossier = Path(tempfile.mkdtemp())
        try:
            (dossier / "sites.json").write_text(json.dumps({
                "parametres": {"pause_nouvel_essai_s": 0, "timeout_s": 3, "seuil_echecs": 1},
                "sites": [{"id": "demo", "url": f"http://127.0.0.1:{site.server_port}/"}]}), encoding="utf-8")
            args = ["--config", str(dossier / "sites.json"), "--etat", str(dossier / "state.json"),
                    "--incidents", str(dossier / "incidents.jsonl")]
            self.assertEqual(monitor.main(args, env={}), 2)
        finally:
            site.shutdown()
            shutil.rmtree(dossier, ignore_errors=True)

    def test_rapport_complet(self):
        dossier = Path(tempfile.mkdtemp())
        try:
            (dossier / "sites.json").write_text(json.dumps({"sites": [
                {"id": "a", "nom": "Plus tard", "url": "https://plus-tard.test", "actif": False}]}), encoding="utf-8")
            code = rapport.main(["--config", str(dossier / "sites.json"), "--etat", str(dossier / "s.json"),
                                 "--incidents", str(dossier / "i.jsonl"),
                                 "--dossier-rapports", str(dossier / "r")], env={})
            self.assertEqual(code, 0)
            self.assertEqual(len(list((dossier / "r").glob("*.md"))), 1)
        finally:
            shutil.rmtree(dossier, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
