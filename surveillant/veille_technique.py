#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
La veille technique quotidienne du Surveillant (le contrôle toutes les 5 minutes reste léger et rapide).

Une fois par jour, vers 6 h :
  domaines     date d'expiration de chaque nom de domaine (alerte 60, 30, 7 et 1 jour avant)
  email        réglages e-mail du domaine (SPF, DKIM, DMARC) : sans eux, tes e-mails finissent en indésirables
  dns          changement suspect des réglages du domaine (adresses du site, des e-mails, serveurs de noms)
  parcours     le parcours d'achat, page par page (page de vente → commande → paiement) : le bouton qui casse
  liens        les liens cassés des pages principales, et l'adresse du QR code imprimé dans le livre
  lenteur      temps de chargement et poids des pages, et la tendance
  liste noire  le domaine est-il signalé comme spammeur ?
Le 1er du mois : le bilan de disponibilité du mois écoulé, par site.

Utilisation : python3 surveillant/veille_technique.py [--dry-run] [--maintenant AAAA-MM-JJTHH:MM]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER.parent))
sys.path.insert(0, str(DOSSIER))

import monitor  # noqa: E402
import rapport  # noqa: E402
from commun import publiques  # noqa: E402
from commun.alertes import Notifier  # noqa: E402

NIVEAUX_DOMAINE = (60, 30, 7, 1)
LISTES_NOIRES = ("dbl.spamhaus.org", "multi.surbl.org")


# --------------------------------------------------------------------------------------
# Les contrôles (chacun reçoit ses outils : tout se teste sans Internet)
# --------------------------------------------------------------------------------------

def controle_domaines(domaines, jour: date, memo: dict, lire=None) -> list:
    alertes = []
    for d in domaines:
        try:
            exp = publiques.domaine_expiration(d["domaine"], lire)
        except Exception as exc:  # noqa: BLE001
            print(f"{d['domaine']} : expiration illisible ({type(exc).__name__})")
            continue
        if not exp:
            continue
        reste = (date.fromisoformat(exp) - jour).days
        niveau = next((n for n in sorted(NIVEAUX_DOMAINE) if reste <= n), None)
        m = memo.setdefault(d["domaine"], {})
        if niveau is not None and (m.get("niveau") is None or niveau < m["niveau"] or m.get("expire") != exp):
            alertes.append(("🔴" if reste <= 7 else "🟠",
                            f"Le nom de domaine {d['domaine']} expire le {date.fromisoformat(exp):%d/%m/%Y} (dans {reste} jour(s))",
                            f"Renouvelle-le chez {d.get('registrar', 'ton bureau d’enregistrement (OVH ?)')} et active le "
                            "renouvellement automatique. Un domaine expiré est souvent racheté en quelques heures par des "
                            "revendeurs : site et e-mails perdus."))
            m["niveau"] = niveau
        elif niveau is None:
            m.pop("niveau", None)
        m["expire"] = exp
    return alertes


def controle_email(domaines, memo: dict, lire=None) -> list:
    alertes = []
    for d in domaines:
        if not d.get("email"):
            continue
        try:
            res = publiques.controle_email_domaine(d["domaine"], d.get("dkim", []), lire)
        except Exception:  # noqa: BLE001
            continue
        manque = sorted(f"{n} : {t}" for n, ok, t in res if not ok)
        avant = memo.get(d["domaine"], [])
        if manque and manque != avant:
            alertes.append(("🟠", f"Réglages e-mail incomplets pour {d['domaine']}",
                            "\n".join(f"   - {x}" for x in manque) + "\n   Que faire : ces réglages se font dans la zone "
                            "DNS du domaine (chez OVH) ; Claude peut te donner les lignes exactes à ajouter."))
        elif avant and not manque:
            alertes.append(("🟢", f"Réglages e-mail de {d['domaine']} : tout est en ordre", ""))
        memo[d["domaine"]] = manque
    return alertes


def controle_dns(domaines, memo: dict, lire=None) -> list:
    alertes = []
    for d in domaines:
        try:
            empreinte = publiques.empreinte_dns(d["domaine"], lire)
        except Exception:  # noqa: BLE001
            continue
        avant = memo.get(d["domaine"])
        if avant and empreinte != avant and all(empreinte.values()):
            changes = [f"   - {g} : {', '.join(avant.get(g, [])) or 'rien'} → {', '.join(empreinte[g]) or 'rien'}"
                       for g in empreinte if empreinte[g] != avant.get(g)]
            alertes.append(("🔴", f"Les réglages du domaine {d['domaine']} ont changé",
                            "\n".join(changes) + "\n   Si ce n'est pas toi (ou Claude) : quelqu'un a peut-être pris la main "
                            "sur le domaine. Change tout de suite le mot de passe du compte OVH et active la double "
                            "authentification, puis préviens Claude. Si c'est voulu, ignore ce message."))
        if all(empreinte.values()) or not avant:
            memo[d["domaine"]] = empreinte
    return alertes


def controle_parcours(parcours, memo: dict, charger) -> list:
    alertes = []
    for p in parcours:
        if not p.get("actif"):
            continue
        casse = None
        for etape in p["etapes"]:
            try:
                r = charger(etape["url"])
            except Exception as exc:  # noqa: BLE001
                casse = (etape, f"page injoignable ({type(exc).__name__})")
                break
            if r["statut"] >= 400:
                casse = (etape, f"erreur {r['statut']}")
                break
            texte = r["corps"]
            if etape.get("mot_cle") and etape["mot_cle"].lower() not in html.unescape(texte).lower():
                casse = (etape, f"le mot « {etape['mot_cle']} » n'est plus sur la page")
                break
        cle = p["nom"]
        if casse and memo.get(cle) != casse[0]["nom"]:
            alertes.append(("🔴", f"Parcours d'achat « {p['nom']} » cassé à l'étape « {casse[0]['nom']} »",
                            f"   {casse[0]['url']} : {casse[1]}.\n   La page d'accueil peut très bien marcher pendant que "
                            "les ventes sont bloquées ici. Vérifie le parcours toi-même depuis ton téléphone."))
            memo[cle] = casse[0]["nom"]
        elif not casse and memo.get(cle):
            alertes.append(("🟢", f"Parcours d'achat « {p['nom']} » de nouveau complet", ""))
            memo.pop(cle, None)
    return alertes


def liens_de(corps: str, base: str) -> list:
    liens = set()
    for h in re.findall(r'href=["\']([^"\'#]+)["\']', corps, flags=re.IGNORECASE):
        if h.startswith(("mailto:", "tel:", "javascript:")):
            continue
        liens.add(urllib.parse.urljoin(base, html.unescape(h)))
    return sorted(liens)


def controle_liens(sites, qr, memo: dict, charger, max_liens=40) -> list:
    casses = []
    for s in sites:
        try:
            r = charger(s["url"])
        except Exception:  # noqa: BLE001
            continue   # le contrôle toutes les 5 minutes s'en occupe
        for lien in liens_de(r["corps"], r.get("url_finale", s["url"]))[:max_liens]:
            try:
                statut = charger(lien)["statut"]
            except Exception:  # noqa: BLE001
                statut = 0
            if statut >= 400 or statut == 0:
                casses.append((s.get("nom", s["id"]), lien, statut))
    for q in qr:
        try:
            r = charger(q["url"])
            ok = r["statut"] < 400 and (not q.get("destination") or monitor.meme_destination(r["url_finale"], q["destination"]))
        except Exception:  # noqa: BLE001
            ok, r = False, {"statut": 0}
        if not ok:
            casses.append((q["nom"], q["url"], r.get("statut", 0)))
    cles = sorted(f"{n}|{l}" for n, l, _ in casses)
    nouveaux = [c for c in casses if f"{c[0]}|{c[1]}" not in memo.get("liens", [])]
    memo["liens"] = cles
    if not nouveaux:
        return []
    return [("🟠", f"{len(nouveaux)} lien(s) cassé(s)",
             "\n".join(f"   - {n} : {l} ({'injoignable' if st == 0 else 'erreur ' + str(st)})" for n, l, st in nouveaux)
             + "\n   Un lien imprimé (QR code du livre) doit marcher pour toujours : si l'adresse change, mets une "
               "redirection.")]


def controle_lenteur(sites, memo: dict, charger, seuil_ms=3000, seuil_ko=3000) -> list:
    alertes = []
    for s in sites:
        try:
            r = charger(s["url"])
        except Exception:  # noqa: BLE001
            continue
        poids = len(r["corps"].encode("utf-8")) // 1024
        hist = memo.setdefault(s["id"], [])
        hist.append(r["ms"])
        del hist[:-30]
        moyenne = sum(hist) / len(hist)
        lent = len(hist) >= 3 and moyenne > seuil_ms
        if lent and not memo.get(f"{s['id']}:signale"):
            alertes.append(("🟠", f"{s.get('nom', s['id'])} est lent ({moyenne / 1000:.1f} s en moyenne)",
                            f"   Poids de la page : {poids} Ko. Un site lent perd des visiteurs et recule dans Google. "
                            "Pistes : images trop lourdes, trop d'extensions ; Claude peut regarder."))
            memo[f"{s['id']}:signale"] = True
        elif not lent:
            memo.pop(f"{s['id']}:signale", None)
        if poids > seuil_ko:
            print(f"{s.get('nom')} : page lourde ({poids} Ko)")
    return alertes


def controle_liste_noire(domaines, memo: dict, lire=None) -> list:
    alertes = []
    for d in domaines:
        signale = []
        for liste in LISTES_NOIRES:
            try:
                rep = publiques.dns(f"{d['domaine']}.{liste}", "A", lire)
            except Exception:  # noqa: BLE001
                continue
            # 127.255.255.x = la liste refuse de répondre au service DNS public : on ne conclut rien
            if any(r.startswith("127.") and not r.startswith("127.255.255.") for r in rep):
                signale.append(liste)
        avant = memo.get(d["domaine"], [])
        if signale and signale != avant:
            alertes.append(("🔴", f"Le domaine {d['domaine']} est sur une liste anti-spam ({', '.join(signale)})",
                            "   Tes e-mails risquent de ne plus arriver. Causes fréquentes : envoi en nombre sans "
                            "désinscription, site piraté. Préviens Claude : il t'aide à demander le retrait."))
        memo[d["domaine"]] = signale
    return alertes


def bilan_mensuel(config, params, etat, incidents, maintenant: datetime) -> str:
    premier = maintenant.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    jours = (premier - (premier - timedelta(days=1)).replace(day=1)).days
    texte = rapport.construire_rapport(config, params, etat, incidents, {}, premier, jours=jours)
    return texte.replace("# Rapport de santé", "# Bilan du mois — disponibilité des sites").replace(
        "Ce rapport arrive chaque lundi : il prouve que la surveillance tourne. Si tu n'en reçois pas un lundi, "
        "préviens Claude.", "Le chiffre de disponibilité du mois : un bon argument pour tes clients (et pour APMvie Pilote).")


# --------------------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------------------

def main(argv=None, env=None, lire=None, charger=None) -> int:
    p = argparse.ArgumentParser(description="Veille technique quotidienne du Surveillant")
    p.add_argument("--config", default=str(DOSSIER / "sites.json"))
    p.add_argument("--etat", default=str(DOSSIER / "veille_technique.json"))
    p.add_argument("--etat-sites", default=str(DOSSIER / "state.json"))
    p.add_argument("--incidents", default=str(DOSSIER / "incidents.jsonl"))
    p.add_argument("--rapports", default=str(DOSSIER / "rapports"))
    p.add_argument("--maintenant")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    config = monitor.charger_config(args.config)
    params = monitor.parametres(config)
    maintenant = datetime.fromisoformat(args.maintenant).replace(tzinfo=timezone.utc) if args.maintenant else monitor.maintenant_utc()
    jour = maintenant.astimezone(monitor.fuseau(params)).date() if monitor.fuseau(params) else maintenant.date()
    chemin = Path(args.etat)
    etat = json.loads(chemin.read_text(encoding="utf-8")) if chemin.exists() else {}
    charger = charger or (lambda url: monitor.charger_page(url, params["timeout_s"]))

    domaines = [d for d in config.get("domaines", []) if d.get("actif")]
    sites = [s for s in config["sites"] if s.get("actif", True)]
    alertes = []
    alertes += controle_domaines(domaines, jour, etat.setdefault("domaines", {}), lire)
    alertes += controle_email(domaines, etat.setdefault("email", {}), lire)
    alertes += controle_dns(domaines, etat.setdefault("dns", {}), lire)
    alertes += controle_parcours(config.get("parcours", []), etat.setdefault("parcours", {}), charger)
    alertes += controle_liens(sites, [q for q in config.get("qr", []) if q.get("actif")], etat.setdefault("liens", {}), charger)
    alertes += controle_lenteur(sites, etat.setdefault("lenteur", {}), charger)
    alertes += controle_liste_noire(domaines, etat.setdefault("liste_noire", {}), lire)

    notifier = Notifier(env=env, dry_run=args.dry_run)
    code = 0
    if alertes:
        graves = [a for a in alertes if a[0] == "🔴"]
        corps = "\n\n".join(f"{e} {t}" + (f"\n{d}" if d else "") for e, t, d in alertes)
        print(corps)
        if not notifier.envoyer({"sujet": f"🛠️ Le Surveillant — veille technique : {len(alertes)} point(s)",
                                 "corps": corps + "\n\nTransmets ce message à Claude pour être guidé.",
                                 "push_titre": "Le Surveillant", "push_texte": alertes[0][1][:200],
                                 "priorite": 4 if graves else 3, "tags": ["hammer_and_wrench"], "sms": None}):
            code = 2
    # Le 1er du mois : bilan de disponibilité du mois écoulé
    mois = jour.strftime("%Y-%m")
    if jour.day == 1 and etat.get("bilan") != mois:
        texte = bilan_mensuel(config, params, monitor.lire_etat(Path(args.etat_sites)),
                              rapport.lire_incidents(Path(args.incidents)), maintenant)
        if notifier.envoyer({"sujet": f"📊 Le Surveillant — bilan du mois de tes sites", "corps": texte,
                             "push_titre": "Le Surveillant", "push_texte": "Le bilan du mois de tes sites est arrivé.",
                             "priorite": 2, "tags": ["bar_chart"], "sms": None}):
            etat["bilan"] = mois
            if not args.dry_run:
                Path(args.rapports).mkdir(parents=True, exist_ok=True)
                (Path(args.rapports) / f"mois-{(jour - timedelta(days=1)):%Y-%m}.md").write_text(texte, encoding="utf-8")
    if not args.dry_run:
        chemin.write_text(json.dumps(etat, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
