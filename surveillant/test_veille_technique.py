#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests de la veille technique du Surveillant. Depuis ce dossier : python3 -m unittest -v test_veille_technique"""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI.parent))
sys.path.insert(0, str(ICI))

import veille_technique as v  # noqa: E402

D = date.fromisoformat
DOM = [{"domaine": "agirpourmavie.com", "actif": True, "email": True, "dkim": ["google"], "registrar": "OVH"}]


class FauxDNS:
    """Répond aux requêtes RDAP et DNS publiques à partir d'un dictionnaire modifiable."""

    def __init__(self):
        self.expiration = "2027-01-05"
        self.txt = {"agirpourmavie.com": ['"v=spf1 include:_spf.google.com ~all"'], "_dmarc.agirpourmavie.com": [],
                    "google._domainkey.agirpourmavie.com": ['"v=DKIM1; p=MIIB"']}
        self.a = {"agirpourmavie.com": ["1.2.3.4"]}
        self.mx = ["1 smtp.google.com."]
        self.ns = ["dns1.ovh.net.", "ns1.ovh.net."]
        self.noire = {}

    def __call__(self, url):
        if "rdap" in url:
            return {"events": [{"eventAction": "expiration", "eventDate": self.expiration + "T00:00:00Z"}]}
        nom = url.split("name=")[1].split("&")[0]
        genre = url.split("type=")[1]
        codes = {"A": 1, "NS": 2, "MX": 15, "TXT": 16}
        if genre == "TXT":
            vals = self.txt.get(nom, [])
        elif genre == "A":
            vals = self.noire.get(nom) or self.a.get(nom, [])
        elif genre == "MX":
            vals = self.mx
        else:
            vals = self.ns
        return {"Answer": [{"type": codes[genre], "data": x} for x in vals]}


class TestControles(unittest.TestCase):
    def test_domaine_niveaux(self):
        dns, memo = FauxDNS(), {}
        self.assertEqual(v.controle_domaines(DOM, D("2026-10-13"), memo, dns), [])          # 84 jours : rien
        a = v.controle_domaines(DOM, D("2026-11-10"), memo, dns)                              # 56 jours
        self.assertIn("expire le 05/01/2027", a[0][1])
        self.assertEqual(v.controle_domaines(DOM, D("2026-11-11"), memo, dns), [])          # pas de répétition
        self.assertEqual(v.controle_domaines(DOM, D("2026-12-30"), memo, dns)[0][0], "🔴")  # 6 jours
        dns.expiration = "2028-01-05"                                                         # renouvelé
        self.assertEqual(v.controle_domaines(DOM, D("2026-12-31"), memo, dns), [])
        self.assertNotIn("niveau", memo["agirpourmavie.com"])

    def test_email_et_retour(self):
        dns, memo = FauxDNS(), {}
        a = v.controle_email(DOM, memo, dns)
        self.assertIn("DMARC : absent", a[0][2])
        self.assertEqual(v.controle_email(DOM, memo, dns), [])
        dns.txt["_dmarc.agirpourmavie.com"] = ['"v=DMARC1; p=none"']
        self.assertEqual(v.controle_email(DOM, memo, dns)[0][0], "🟢")

    def test_dns_change(self):
        dns, memo = FauxDNS(), {}
        self.assertEqual(v.controle_dns(DOM, memo, dns), [])                                 # première prise d'empreinte
        dns.mx = ["10 mail.pirate.example."]
        a = v.controle_dns(DOM, memo, dns)
        self.assertIn("MX : 1 smtp.google.com. → 10 mail.pirate.example.", a[0][2])
        self.assertEqual(v.controle_dns(DOM, memo, dns), [])

    def test_liste_noire(self):
        dns, memo = FauxDNS(), {}
        dns.noire["agirpourmavie.com.dbl.spamhaus.org"] = ["127.255.255.254"]               # refus de répondre
        self.assertEqual(v.controle_liste_noire(DOM, memo, dns), [])
        dns.noire["agirpourmavie.com.dbl.spamhaus.org"] = ["127.0.1.2"]
        self.assertIn("dbl.spamhaus.org", v.controle_liste_noire(DOM, memo, dns)[0][1])


def page(statut=200, corps="", ms=200, url=None):
    return {"statut": statut, "corps": corps, "ms": ms, "url_finale": url}


class TestPages(unittest.TestCase):
    def test_parcours(self):
        pages = {"https://f.fr": page(corps="Découvre la formation"), "https://f.fr/commande": page(corps="<b>Payer</b> maintenant")}
        charger = lambda u: pages[u]
        parcours = [{"nom": "Formation", "actif": True, "etapes": [{"nom": "Vente", "url": "https://f.fr"},
                                                                      {"nom": "Commande", "url": "https://f.fr/commande", "mot_cle": "payer"}]}]
        memo = {}
        self.assertEqual(v.controle_parcours(parcours, memo, charger), [])
        pages["https://f.fr/commande"] = page(corps="Erreur du module de paiement")
        a = v.controle_parcours(parcours, memo, charger)
        self.assertIn("cassé à l'étape « Commande »", a[0][1])
        self.assertEqual(v.controle_parcours(parcours, memo, charger), [])
        pages["https://f.fr/commande"] = page(corps="Payer")
        self.assertEqual(v.controle_parcours(parcours, memo, charger)[0][0], "🟢")

    def test_liens_et_qr(self):
        pages = {"https://livre.fr": page(corps='<a href="/a">a</a> <a href="https://x.fr/b">b</a> <a href="mailto:x@y">m</a>', url="https://livre.fr"),
                 "https://livre.fr/a": page(), "https://x.fr/b": page(404), "https://qr.fr": page(url="https://livre.fr")}
        charger = lambda u: pages[u]
        memo = {}
        a = v.controle_liens([{"id": "l", "nom": "Livre", "url": "https://livre.fr"}],
                             [{"nom": "QR du livre", "url": "https://qr.fr", "destination": "https://livre.fr"}], memo, charger)
        self.assertIn("https://x.fr/b (erreur 404)", a[0][2])
        self.assertNotIn("QR du livre :", a[0][2])
        self.assertEqual(v.controle_liens([{"id": "l", "nom": "Livre", "url": "https://livre.fr"}], [], memo, charger), [])

    def test_lenteur(self):
        memo = {}
        lent = lambda u: page(ms=4500, corps="x")
        s = [{"id": "l", "nom": "Livre", "url": "https://livre.fr"}]
        self.assertEqual(v.controle_lenteur(s, memo, lent), [])
        self.assertEqual(v.controle_lenteur(s, memo, lent), [])
        self.assertIn("lent (4.5 s", v.controle_lenteur(s, memo, lent)[0][1])
        self.assertEqual(v.controle_lenteur(s, memo, lent), [])


class TestDeBoutEnBout(unittest.TestCase):
    def test_parcours_complet(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        conf = json.loads((ICI / "sites.json").read_text(encoding="utf-8"))
        conf["domaines"][0]["actif"] = True
        (d / "sites.json").write_text(json.dumps(conf), encoding="utf-8")
        envoyes = []
        ancien = v.Notifier

        class N:
            def __init__(self, env=None, dry_run=False):
                pass

            def envoyer(self, m, avec_sms=False):
                envoyes.append(m)
                return True
        v.Notifier = N
        self.addCleanup(setattr, v, "Notifier", ancien)
        args = ["--config", str(d / "sites.json"), "--etat", str(d / "vt.json"), "--etat-sites", str(d / "st.json"),
                "--incidents", str(d / "inc.jsonl"), "--rapports", str(d / "rap")]
        dns = FauxDNS()
        dns.expiration = "2026-11-20"
        self.assertEqual(v.main(args + ["--maintenant", "2026-11-01T05:00"], env={}, lire=dns, charger=lambda u: page()), 0)
        sujets = [m["sujet"] for m in envoyes]
        self.assertIn("🛠️ Le Surveillant — veille technique : 2 point(s)", sujets)   # domaine + DMARC
        self.assertIn("📊 Le Surveillant — bilan du mois de tes sites", sujets)
        self.assertTrue((d / "rap" / "mois-2026-10.md").exists())
        v.main(args + ["--maintenant", "2026-11-02T05:00"], env={}, lire=dns, charger=lambda u: page())
        self.assertEqual(len(envoyes), 2)


if __name__ == "__main__":
    unittest.main()
