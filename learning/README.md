# Apprendre à concevoir un microcontrôleur RISC-V

Ce dossier accompagne la milestone MCU du projet. Il explique les concepts derrière les décisions d'architecture avec un vocabulaire accessible, des exemples et les véritables compromis d'ingénierie.

Les chapitres servent à apprendre. Le contrat normatif reste [le contrat MCU](../docs/open-mcu-milestone-definition.md).

## Parcours conseillé

1. [RV32IMC et le contrat logiciel](01-isa-et-logiciel.md)
2. [Le cœur et son pipeline](02-coeur-et-pipeline.md)
3. [Démarrage, interruptions et débogage](03-controle-et-demarrage.md)
4. [L’interconnexion OBI](04-interconnexion-obi.md)
5. [La mémoire et la chaîne de démarrage](05-memoire-et-demarrage.md)
6. [Périphériques, broches et programmation USB-C](06-peripheriques-et-carte.md)
7. [Technologie, corners et preuve de fréquence](07-technologie-et-frequence.md)
8. [Vérification, releases et séparation des dépôts](08-verification-et-releases.md)

Le parcours couvre maintenant les huit décisions du contrat. Il sera enrichi avec les résultats mesurés pendant l’implémentation et le bring-up.

## Quelques repères

- Une **instruction** est un ordre compris par le processeur.
- Un **registre** est une petite mémoire très proche du calcul.
- Un **cycle** est un battement de l'horloge.
- Le **RTL** décrit les registres et la logique qui seront fabriqués.
- Le **timing** vérifie que les signaux arrivent avant le battement suivant.
- Une **interface** est un contrat électrique et temporel entre deux blocs.

Une fréquence annoncée dépend toujours d'une technologie, des mémoires, des contraintes et du niveau de preuve. Un objectif écrit ne constitue pas encore une mesure.
