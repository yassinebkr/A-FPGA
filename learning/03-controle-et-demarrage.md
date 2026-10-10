# 3 — Contrôle, démarrage et débogage

## Du reset au firmware

Le reset place les registres dans un état connu. Sa sortie est synchronisée avec l'horloge pour que le circuit reprenne proprement.

Le démarrage prévu suit cette chaîne :

1. exécution de la ROM interne ;
2. initialisation minimale ;
3. lecture du firmware depuis la Flash SPI ;
4. contrôle de l'image ;
5. copie en SRAM ;
6. lancement du firmware.

JTAG fournit un chemin de récupération lorsque la Flash ou le chargeur échoue.

## Interruptions et exceptions

Une interruption vient d'un événement extérieur à l'instruction, par exemple la réception UART. Le cœur sauvegarde son point d'exécution, traite l'événement puis reprend.

Une exception vient de l'instruction courante : encodage inconnu, accès désaligné ou violation de protection. Une exception précise conserve la cause, l'adresse utile et l'instruction responsable.

Le mode vectorisé associe rapidement chaque cause à un gestionnaire. Le mode direct utilise une entrée commune.

## Protection PMP

Les huit régions PMP donnent des droits de lecture, écriture et exécution à des plages d'adresses. Elles peuvent protéger la ROM, empêcher l'exécution dans une zone de données ou isoler une région réservée. PMP fournit une protection utile sans introduire toute la traduction mémoire d'une MMU.

## Pourquoi JTAG est essentiel

JTAG permet d'arrêter le cœur, lire ses registres, inspecter la mémoire, poser des points d'arrêt et charger un programme. Son coût en logique et en broches réduit fortement le risque de recevoir une puce impossible à diagnostiquer.

Les compteurs 64 bits de cycles et d'instructions terminées rendent les performances mesurables. Les protections lourdes comme le lockstep et l'ECC généralisé seront étudiées lorsqu'un besoin concret justifiera leur surface et leur vérification.
