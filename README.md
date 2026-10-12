# Rsync GUI

App macOS à deux panneaux (source / destination, façon FileZilla) autour de `rsync`,
pensée pour copier de gros fichiers vers un NAS SMB qui se déconnecte parfois.

- Navigation, sélection multiple, glisser-déposer entre les deux panneaux
- Reprises automatiques après une coupure réseau (erreurs -51, 100060, « Bad file descriptor »)
- Remontage automatique du partage SMB (identifiants du Trousseau)
- Reprise des gros fichiers (`--inplace`) grâce à rsync 3 embarqué
- rsync se met à jour tout seul (vérification toutes les 24 h)
- Tâches enregistrées, simulation (`-n`), mode miroir optionnel
- Historique des transferts conservé, reprise des transferts interrompus, pause / reprise / priorité
- Icône dans la barre de menus : clic = miniature, clic droit = menu détaillé ; la fenêtre se range sans quitter
- Design « Liquid Glass » (matières translucides, capsules, clair et sombre)

## Construire

```bash
scripts/build.sh            # app + DMG dans build/
scripts/build.sh --install  # + copie dans ~/Applications
scripts/build.sh --rebuild-rsync
```

Prérequis : Xcode (ou ses outils en ligne de commande), Python 3, Pillow (uniquement pour régénérer le logo).

Résultat : `build/Rsync GUI.app` et `build/Rsync-GUI-<version>.dmg`.

## Arborescence

| Chemin | Rôle |
|---|---|
| `src/app.py` | Serveur local + logique rsync (reprises, remontage, pause, historique, mise à jour) |
| `src/web/` | Interface : `index.html` (fenêtre principale) et `mini.html` (miniature) |
| `src/RsyncGUI.swift` | Fenêtre native (WKWebView), lance `app.py`, boîtes de dialogue |
| `src/Info.plist` | Métadonnées de l'app |
| `assets/make_icon.py` | Génère `AppIcon.icns` et `logo.png` |
| `scripts/build.sh` | Compile rsync, l'app, signe (ad hoc) et crée le DMG |
| `VERSION` | Numéro de version de l'app |
| `build/` | Sorties de build (ignoré par git) |

## Fonctionnement de rsync

- Au build : le code source officiel (download.samba.org) est compilé avec uniquement des bibliothèques système,
  puis placé dans `Contents/Resources/bin/rsync`.
- À l'usage : une fois par jour, l'app cherche une version plus récente, la télécharge en HTTPS,
  la compile (Xcode requis) vers `~/Library/Application Support/Rsync GUI/bin/rsync`
  et utilise automatiquement la plus récente. L'app signée n'est jamais modifiée.
- Bouton « Mettre à jour rsync » en haut à droite pour forcer la vérification.

## Données

- `~/.rsync-gui/jobs.json` : tâches enregistrées
- `~/.rsync-gui/state.json` : date de la dernière vérification
- `~/.rsync-gui/server.log` : journal du serveur interne

## Installation sur un autre Mac

L'app est signée « ad hoc » (sans compte développeur). Après copie depuis le DMG sur une autre machine,
faire clic droit → Ouvrir la première fois. Au premier lancement, macOS demande l'accès aux dossiers,
volumes réseau et disques externes : accepter.

## Branches

- `main` : dernière version validée
- `design/liquid-glass` : refonte visuelle (verre translucide, icônes vectorielles, barre de chemin)

Revenir en arrière : `git checkout main && scripts/build.sh --install`.

## Tester sans toucher à ses données

Lancer une copie de l'app avec un autre identifiant et un dossier personnel factice :
`HOME=/tmp/test RSYNCGUI_APPEARANCE=dark "…/Rsync GUI.app/Contents/MacOS/RsyncGUI"`
(`RSYNCGUI_APPEARANCE` accepte `dark` ou `light`).
