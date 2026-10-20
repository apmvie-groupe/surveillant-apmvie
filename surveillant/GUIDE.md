# Le Surveillant — surveillance des sites du Groupe APMvie

Un système gratuit qui vérifie tes sites toutes les 5 minutes et te prévient par **e-mail, notification sur ton téléphone et SMS** quand quelque chose ne va pas. Il est fait pour couvrir **toutes tes entreprises** : pour surveiller un nouveau site, il suffit d'ajouter un bloc dans `sites.json`.

## Comment ça marche

1. **Le surveillant** (`monitor.py`) visite chaque site actif toutes les 5 minutes. Il vérifie que la page répond, que le texte attendu est dessus, que les redirections mènent au bon endroit et que le certificat HTTPS (le cadenas) n'expire pas bientôt.
2. **Le filtre anti-fausse alerte** : un site doit rater **2 contrôles de suite** (donc environ 10 minutes) avant qu'une alerte parte. Un raté isolé ne dérange personne.
3. **L'alerte** arrive avec le problème expliqué simplement, la piste la plus probable, et **un message à copier-coller à Claude**.
4. **Le correcteur, c'est Claude** : tu lui colles le message, il diagnostique et corrige. Tu valides avant toute publication. Claude n'est pas lancé automatiquement : il intervient quand tu lui transmets l'alerte.
5. **Le superviseur** : un journal des pannes (`incidents.jsonl`) et un **rapport de santé chaque lundi** par e-mail. Si un lundi tu ne reçois pas le rapport, c'est que la surveillance elle-même s'est arrêtée : préviens Claude.

Tu reçois aussi : un **rappel** si une panne dure plus d'une heure (puis toutes les 6 h), un message **« rétabli »** quand c'est réparé, et une alerte **certificat** à 14, 7 et 3 jours avant son expiration.

## Ses tâches d'expert

Une fois par jour vers 6 h (automatisation « Le Surveillant — veille quotidienne », `veille_technique.py`) :

- **les noms de domaine** : alerte 60, 30, 7 et 1 jour avant leur expiration ;
- **les réglages e-mail du domaine** (SPF, DKIM, DMARC) : sans eux, tes e-mails finissent en indésirables ;
- **un changement suspect des réglages du domaine** (adresses du site, des e-mails, serveurs de noms) ;
- **le parcours d'achat**, page par page (page de vente → commande → paiement) ;
- **les liens cassés** des pages principales, et l'adresse du QR code imprimé dans le livre ;
- **la lenteur** des pages et sa tendance ;
- **la liste noire** des spammeurs.

Le 1er du mois : le bilan de disponibilité de chaque site.

## Les fichiers

| Fichier | Rôle |
|---|---|
| `sites.json` | La liste des sites à surveiller (c'est le seul que tu auras à modifier) |
| `monitor.py` | Le surveillant (les alertes sont dans `commun/alertes.py`, partagé avec les autres agents) |
| `rapport.py` | Le rapport du lundi |
| `state.json` | La mémoire du système (vide quand tout va bien) |
| `incidents.jsonl` | L'historique des pannes |
| `rapports/` | Les rapports hebdomadaires |
| `.github/workflows/surveillant*.yml` (à la racine) | Le planning automatique (toutes les 5 min, et le lundi) |
| `test_monitor.py` | Les tests automatiques |

## Mise en place (environ 30 minutes, à faire avec Claude)

**1. Un dépôt GitHub public, pour lui seul.** Les autres agents vont dans un dépôt privé (voir le README à la racine). Public est nécessaire : GitHub offre les contrôles automatiques gratuits et illimités sur les dépôts publics, alors qu'un dépôt privé n'a que 2 000 minutes par mois, insuffisant pour un contrôle toutes les 5 minutes (environ 8 600 par mois). Ce que le public pourrait voir : l'adresse de tes sites et l'historique des pannes. **Tes mots de passe et clés ne sont jamais dans le dépôt** (voir l'étape 2). Si tu préfères un dépôt privé, il faut espacer les contrôles à 30 minutes (ligne `cron` dans `.github/workflows/surveillant.yml`).

**2. Les « secrets ».** Dans le dépôt : *Settings → Secrets and variables → Actions → New repository secret*. Ils sont chiffrés, invisibles de tous, y compris du public.

| Secret | À quoi il sert | Obligatoire |
|---|---|---|
| `SMTP_USER` | Ton adresse Gmail d'envoi | oui, pour l'e-mail |
| `SMTP_PASSWORD` | Un **mot de passe d'application** Google (pas ton vrai mot de passe) : compte Google → Sécurité → Validation en 2 étapes → Mots de passe des applications | oui, pour l'e-mail |
| `ALERT_EMAIL_TO` | L'adresse qui reçoit les alertes (par défaut, la même) | non |
| `NTFY_TOPIC` | Le « canal » de notification sur ton téléphone : invente un nom long et illisible (ex. `apmvie-x7k2q9-alerte`) | oui, pour le push |
| `FREE_SMS_USER`, `FREE_SMS_KEY` | Ton identifiant Free et la clé de l'option « Notifications par SMS » (espace abonné Free → Mes options). **À ajouter quand tu seras chez Free** | pour le SMS |

Pour le push : installe l'appli gratuite **ntfy** sur ton téléphone et abonne-toi au même nom de canal que `NTFY_TOPIC`. Quiconque connaît ce nom peut lire les notifications : garde-le secret.

**3. Le test.** Onglet *Actions → Surveillance des sites → Run workflow*, coche « Envoyer une alerte de test ». Tu dois recevoir un e-mail et une notification (et un SMS si Free est configuré). Tant que ce test n'a pas réussi, on ne considère pas le système en place.

**4. Activer les sites.** Dans `sites.json`, passe `"actif": true` pour un site seulement **quand il est réellement en ligne**, sinon tu recevras des alertes pour rien. Pour la page de paiement Systeme.io, mets l'adresse exacte de la page et un mot qu'on y voit (champ `mot_cle`).

## Que faire quand une alerte arrive

1. Lis le problème et la piste dans l'alerte.
2. Copie le bloc **« Message à copier-coller à Claude »** et colle-le dans une conversation avec Claude.
3. Claude diagnostique et te propose le correctif. **Tu valides avant toute publication**, surtout sur une page de vente.
4. Tu reçois le message « rétabli » quand c'est réparé.

## Ce qui est réparable, ou pas

- **Site vitrine sur OVH** (code créé avec Claude) : Claude peut corriger.
- **Pages Systeme.io, paiement Stripe** : le système détecte la panne et te prévient, Claude t'aide à diagnostiquer, mais si ce sont leurs serveurs qui sont en panne, personne ne peut réparer à leur place.
- **Redirections de domaines** : Claude te guide pas à pas dans l'espace client OVH.

## Limites à connaître

- GitHub ne garantit pas l'heure exacte : un contrôle « toutes les 5 minutes » arrive souvent avec 5 à 15 minutes de retard, parfois plus aux heures chargées. Une panne est donc signalée en général en 10 à 30 minutes. Le **signal de vie** (ci-dessous) prévient si les contrôles s'arrêtent complètement.
- Certains sites (souvent ceux protégés par Cloudflare) demandent aux robots de prouver qu'ils sont humains. Le Surveillant le reconnaît : ce n'est pas compté comme une panne (le site répond), mais le contenu de la page n'est alors pas vérifié. Il l'écrit dans son journal.
- Le système voit ce qu'un visiteur voit depuis un serveur aux États-Unis. Il ne remplace pas un test humain d'un achat complet : fais-en un de temps en temps.

## Le relais avec les autres agents

- **Le Remplaçant** (dépôt privé) vérifie toutes les 3 heures que Le Surveillant tourne. S'il s'est arrêté, il le relance (secret `JETON_DEPOT_SURVEILLANT`) ; si ça ne repart pas, il contrôle lui-même les sites à sa place, avec la copie de `sites.json` du dépôt privé. Il réveille aussi Le Surveillant si GitHub l'a mis en pause.
- **Le signal de vie** : à chaque contrôle, Le Surveillant prévient un service extérieur gratuit (secret `SIGNAL_DE_VIE_SURVEILLANT`, voir remplacant/GUIDE.md). Si ce service ne reçoit plus rien, c'est lui qui t'écrit : il voit même une panne de GitHub tout entier.
- **Quand tu ajoutes un site**, il faut le mettre dans les deux copies de `sites.json` (dépôt public et dépôt privé) : Claude le fait pour toi.

## Ajouter une entreprise ou un site

Copie un bloc de `sites.json`, change `id`, `nom`, `entreprise`, `url`, puis `"actif": true`. Ou dis simplement à Claude : « ajoute ce site à la surveillance ».
