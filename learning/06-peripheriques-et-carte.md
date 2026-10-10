# 6 — Périphériques, broches et programmation USB-C

## Du registre logiciel à la broche

Un périphérique contient des registres accessibles par le bus. Le logiciel configure ces registres, puis le bloc transforme les valeurs numériques en signaux temporels sur les broches.

Le premier MCU regroupe UART, SPI maître, I²C maître, huit GPIO, un timer général et un watchdog. Chaque bloc possède des interruptions afin que le processeur puisse travailler entre deux événements.

## UART

UART transmet les bits sur une ligne TX et les reçoit sur RX. Le format 8N1 signifie huit bits de données, aucune parité et un bit d'arrêt. Une FIFO de 16 octets absorbe de petites rafales sans interrompre le cœur à chaque caractère.

UART servira à la console, aux diagnostics et au chargement de firmware par la carte de développement.

## SPI

SPI emploie une horloge, une ligne dans chaque direction et un signal de sélection par cible. Les quatre modes décrivent la polarité et l'instant d'échantillonnage de l'horloge.

Deux sélections permettent de connecter la Flash de démarrage et un périphérique utilisateur au même contrôleur. Cette mutualisation économise de la logique et des broches.

## I²C

I²C utilise SDA et SCL en open-drain : les composants tirent la ligne vers zéro et des résistances externes la ramènent vers un. Plusieurs composants peuvent ainsi partager deux fils.

Le premier profil agit comme maître à 100 ou 400 kHz et accepte le clock stretching, qui permet à une cible lente de retenir l'horloge. Les cas multi-maître et esclave demandent davantage d'arbitrage et sont différés.

## GPIO, timer et watchdog

Les huit GPIO deviennent des entrées ou sorties et peuvent déclencher une interruption. Le timer produit des événements programmés. Le watchdog réinitialise le MCU lorsque le firmware cesse de signaler qu'il fonctionne correctement.

## Programmer par USB-C sans USB natif

La carte comportera un connecteur USB-C et un pont USB–UART. Pour l'utilisateur, un seul câble permet de charger le firmware et lire la console.

Le bootloader UART en ROM reçoit une image, contrôle son format et son CRC, puis l'écrit dans la Flash SPI. JTAG reste disponible pour arrêter le cœur, inspecter la mémoire et récupérer une puce dont le firmware est défectueux.

Cette organisation évite d'intégrer immédiatement une PHY USB, ses contraintes analogiques, son horloge précise et ses tests électriques. Une future révision pourra ajouter USB Device Full-Speed et un bootloader DFU.

## Horloge et boîtier

Le premier silicium reçoit une horloge externe. Les périphériques lents utilisent des diviseurs numériques. L'absence de PLL interne réduit fortement le risque analogique.

Environ 26 signaux fonctionnels, auxquels s'ajoutent alimentations et masses, forment un budget compatible avec la classe QFN48. Le fournisseur MPW décidera du boîtier exact, des cellules d'IO, des protections ESD et des règles de bonding.
