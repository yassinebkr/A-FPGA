# 2 — Le cœur et son pipeline

## Exécuter une instruction

Le cœur lit l'instruction, la décode, effectue le calcul puis écrit le résultat. Réaliser trop de logique dans un seul cycle allonge le chemin électrique et réduit la fréquence maximale.

## Le pipeline

Des registres séparent le travail en étapes :

| Cycle | Instruction A | Instruction B | Instruction C |
|---|---|---|---|
| 1 | Lecture | | |
| 2 | Décodage | Lecture | |
| 3 | Exécution | Décodage | Lecture |
| 4 | Écriture | Exécution | Décodage |
| 5 | | Écriture | Exécution |

Après le remplissage, plusieurs instructions avancent simultanément.

## Deux ou quatre étapes

Deux étapes demandent moins de registres et simplifient le contrôle, avec davantage de logique par cycle. Quatre étapes raccourcissent les chemins et favorisent une fréquence élevée. Elles introduisent plus de dépendances et d'annulations lors des branchements.

Notre direction utilise quatre étapes. Le nombre d'étages ne prouve toutefois aucune fréquence : mémoires, cellules, placement, fils et contraintes comptent aussi.

## Dépendances et branchements

Si une instruction utilise le résultat de la précédente, le cœur peut attendre ou transmettre directement le résultat entre étapes. Ce transfert, appelé forwarding, améliore les performances et augmente la logique de contrôle.

Un branchement peut invalider les instructions déjà lues. Un pipeline profond paie une pénalité supérieure lorsqu'il s'est engagé dans la mauvaise direction.

## Multiplication et division

Le multiplicateur pipeliné répartit son calcul sur plusieurs cycles pour protéger la fréquence et le débit. Le diviseur itératif traite quelques bits par cycle : il est plus lent et beaucoup plus compact.

CV32E40P est notre candidat principal ; CVE2/Croc sert de contrôle connu. Une enveloppe stable autour du cœur permettra de comparer ou remplacer le cœur sans reconstruire le SoC.
