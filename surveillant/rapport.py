#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rapport hebdomadaire de santé des sites du Groupe APMvie.

Chaque lundi : contrôle tous les sites une dernière fois, calcule la disponibilité des 7 derniers jours
à partir du journal des pannes, enregistre le rapport dans le dossier rapports/ et l'envoie par e-mail.
Ce rapport sert aussi de preuve que la surveillance tourne : si tu n'en reçois pas un lundi, préviens Claude.

Utilisation :
    python3 rapport.py              rapport normal
    python3 rapport.py --dry-run    affiche le rapport sans l'envoyer ni l'enregistrer
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import monitor

DOSSIER = Path(__file__).resolve().parent


def lire_incidents(chemin: Path):
    incidents = []
    if chemin.exists():
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if ligne:
                try:
                    incidents.append(json.loads(ligne))
                except json.JSONDecodeError:
                    continue
    return incidents


def pannes_de_la_periode(site, incidents, etat_site, debut_fenetre, maintenant, seuil):
    """Renvoie (liste des pannes, secondes d'indisponibilité) pour un site sur la fenêtre."""
    pannes = []
    total = 0.0
    for inc in incidents:
        if inc.get("site") != site["id"]:
            continue
        debut = monitor.parse_iso(inc["debut"])
        fin = monitor.parse_iso(inc["fin"])
        if fin <= debut_fenetre or debut >= maintenant:
            continue
        total += (min(fin, maintenant) - max(debut, debut_fenetre)).total_seconds()
        pannes.append({"debut": debut, "fin": fin, "cause": inc.get("cause"), "en_cours": False})
    if etat_site and etat_site.get("premier_echec") and etat_site.get("echecs", 0) >= seuil:
        debut = monitor.parse_iso(etat_site["premier_echec"])
        total += (maintenant - max(debut, debut_fenetre)).total_seconds()
        pannes.append({"debut": debut, "fin": None, "cause": etat_site.get("cause"), "en_cours": True})
    return pannes, total


def construire_rapport(config, params, etat, incidents, resultats, maintenant, jours=7):
    debut_fenetre = maintenant - timedelta(days=jours)
    sites = [s for s in config["sites"] if s.get("actif", True)]
    inactifs = [s for s in config["sites"] if not s.get("actif", True)]
    etat_sites = etat.get("sites", {})
    duree_fenetre = jours * 86400

    lignes = []
    total_pannes = 0
    dispos = []
    detail_pannes = []
    alertes_certificat = []
    for site in sites:
        seuil = site.get("seuil_echecs", params["seuil_echecs"])
        pannes, secondes = pannes_de_la_periode(site, incidents, etat_sites.get(site["id"]),
                                                debut_fenetre, maintenant, seuil)
        dispo = 100.0 * (1 - min(secondes, duree_fenetre) / duree_fenetre)
        dispos.append(dispo)
        total_pannes += len(pannes)
        res = resultats.get(site["id"])
        if res is None:
            etat_actuel = "non contrôlé"
        elif res["ok"]:
            etat_actuel = "OK"
        else:
            libelle = monitor.CAUSES.get(res["cause"], monitor.CAUSES["inconnue"])[0]
            etat_actuel = f"PROBLÈME : {libelle}"
        cert = "-"
        if res and res.get("ssl_jours") is not None:
            cert = f"{int(res['ssl_jours'])} j"
            if res["ssl_jours"] <= max(params["ssl_alerte_jours"]):
                alertes_certificat.append(f"{site.get('nom', site['id'])} : {int(res['ssl_jours'])} jours")
        lignes.append((site.get("nom", site["id"]), site.get("entreprise", ""), etat_actuel,
                       str(len(pannes)), monitor.duree_lisible(secondes), f"{dispo:.2f} %", cert))
        for p in pannes:
            quand = monitor.fmt_local(p["debut"], params)
            if p["en_cours"]:
                detail_pannes.append(f"- {site.get('nom', site['id'])} : panne en cours depuis {quand} ({p['cause']})")
            else:
                duree = monitor.duree_lisible((p["fin"] - p["debut"]).total_seconds())
                detail_pannes.append(f"- {site.get('nom', site['id'])} : {quand}, {duree} ({p['cause']})")

    moyenne = sum(dispos) / len(dispos) if dispos else None
    debut_txt = monitor.fmt_local(debut_fenetre, params)[:10]
    fin_txt = monitor.fmt_local(maintenant, params)[:10]

    sortie = [f"# Rapport de santé — Groupe APMvie — du {debut_txt} au {fin_txt}", ""]
    if not sites:
        sortie.append("Aucun site n'est actif pour l'instant : rien à surveiller. "
                      "Passe « actif » à true dans sites.json quand un site est en ligne.")
    else:
        resume = f"{len(sites)} site(s) surveillé(s), {total_pannes} panne(s) sur la semaine"
        if moyenne is not None:
            resume += f", disponibilité moyenne {moyenne:.2f} %."
        sortie.append("**Résumé :** " + resume)
        sortie += ["", "| Site | Entreprise | État maintenant | Pannes | Temps de panne | Disponibilité | Certificat HTTPS |",
                   "|---|---|---|---|---|---|---|"]
        for ligne in lignes:
            sortie.append("| " + " | ".join(ligne) + " |")
        sortie += ["", "## Pannes de la semaine", ""]
        sortie += detail_pannes if detail_pannes else ["Aucune panne détectée."]
        if alertes_certificat:
            sortie += ["", "## Certificats à surveiller", ""] + [f"- {a}" for a in alertes_certificat]
    if inactifs:
        sortie += ["", "## Sites pas encore surveillés", ""]
        sortie += [f"- {s.get('nom', s['id'])} ({s['url']})" for s in inactifs]
    sortie += ["", "---",
               "Ce rapport arrive chaque lundi : il prouve que la surveillance tourne. "
               "Si tu n'en reçois pas un lundi, préviens Claude."]
    return "\n".join(sortie) + "\n"


def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(description="Rapport hebdomadaire de santé des sites")
    parser.add_argument("--config", default=str(DOSSIER / "sites.json"))
    parser.add_argument("--etat", default=str(DOSSIER / "state.json"))
    parser.add_argument("--incidents", default=str(DOSSIER / "incidents.jsonl"))
    parser.add_argument("--dossier-rapports", default=str(DOSSIER / "rapports"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config = monitor.charger_config(args.config)
    params = monitor.parametres(config)
    maintenant = monitor.maintenant_utc()
    etat = monitor.lire_etat(Path(args.etat))
    incidents = lire_incidents(Path(args.incidents))

    sites = [s for s in config["sites"] if s.get("actif", True)]
    resultats = {}
    if sites:
        with ThreadPoolExecutor(max_workers=min(8, len(sites))) as pool:
            liste = list(pool.map(lambda s: monitor.controler_site(s, params), sites))
        resultats = {s["id"]: r for s, r in zip(sites, liste)}

    texte = construire_rapport(config, params, etat, incidents, resultats, maintenant)
    print(texte)
    if args.dry_run:
        return 0

    dossier = Path(args.dossier_rapports)
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / f"{maintenant.strftime('%Y-%m-%d')}.md").write_text(texte, encoding="utf-8")

    notifier = monitor.Notifier(env=env)
    nb_sites = len(sites)
    message = {
        "sujet": f"📊 Rapport hebdomadaire — Groupe APMvie ({maintenant.strftime('%d/%m/%Y')})",
        "corps": texte,
        "push_titre": "Rapport hebdo APMvie",
        "push_texte": f"{nb_sites} site(s) surveillé(s). Rapport complet dans ta boîte e-mail.",
        "priorite": 2,
        "tags": ["bar_chart"],
        "sms": None,
    }
    notifier.envoyer(message, avec_sms=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
