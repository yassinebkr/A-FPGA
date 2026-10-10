# 8 — Vérification, releases et séparation des dépôts

## Pourquoi plusieurs formes de vérification ?

Un testbench peut reproduire la même erreur que le RTL. Une simulation réussie ne suffit donc pas. La confiance vient de méthodes indépendantes qui observent le circuit sous des angles différents.

Les tests ISA vérifient le langage RISC-V. Un modèle de référence calcule séparément le résultat attendu. Les tests aléatoires explorent des combinaisons difficiles à prévoir. Les propriétés formelles prouvent certaines règles pour tous les états accessibles dans leurs limites.

## Couverture

La couverture de code indique quelles lignes et branches ont été exercées. La couverture fonctionnelle suit les situations prévues par le contrat : exceptions, conflits d'interruptions, modes SPI ou protections PMP.

Un pourcentage élevé peut masquer une fonctionnalité oubliée. Le plan demande donc que chaque exigence possède une preuve et que chaque exclusion soit expliquée.

## CDC, RDC et lint

Le lint détecte des constructions RTL dangereuses ou ambiguës. CDC examine les signaux traversant des horloges différentes. RDC examine les interactions entre domaines de reset.

Même avec une horloge principale, JTAG et les entrées asynchrones créent des frontières qui doivent être synchronisées et vérifiées.

## Équivalence

La synthèse et le placement-routage transforment le circuit. L'équivalence compare le comportement du RTL avec la netlist produite. Les macros mémoire et cellules spéciales demandent des modèles adaptés.

Cette preuve empêche une optimisation d'EDA de modifier silencieusement le fonctionnement.

## Une release de fabrication

Une release associe durablement :

- sources et commits exacts ;
- GDS, netlists et contraintes ;
- rapports de timing et vérifications physiques ;
- firmware et vecteurs de test ;
- pinout, carte et procédure de programmation ;
- versions des outils et conteneurs ;
- licences, provenance et empreintes cryptographiques.

Le fournisseur doit pouvoir reproduire et vérifier ce paquet sans dépendre d'un état local non documenté.

## Bring-up du silicium

À réception des puces, les tests progressent de l'alimentation vers les fonctions complexes : reset, horloge, JTAG, ROM, SRAM, UART, boot SPI, ISA, périphériques, consommation et fréquence.

Les mesures sont comparées aux prédictions. Les différences deviennent des données d'ingénierie pour la révision suivante.

## Rôle des deux dépôts

**A-FPGA** contient la plateforme autonome, les outils, les contrats génériques et les preuves de qualification.

**Open_MC** contient le produit : RTL, firmware, tests, contraintes physiques, carte et releases silicium.

Chaque résultat indique les deux commits. Open_MC commence en privé afin de construire proprement son historique et pourra devenir public après audit des licences, de la provenance et des gates applicables.
