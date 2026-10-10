# 1 — RV32IMC et le contrat logiciel

## L'ISA : le langage public du processeur

L'ISA définit les instructions, les registres visibles par le logiciel et les règles d'exécution. Deux circuits internes différents peuvent exécuter le même programme lorsqu'ils respectent la même ISA. Le logiciel reste stable pendant que le pipeline évolue.

## Décoder RV32IMC

- **RV** désigne RISC-V.
- **32** indique des registres et des adresses de 32 bits.
- **I** fournit les instructions entières fondamentales.
- **M** ajoute multiplication et division.
- **C** ajoute des encodages compressés de 16 bits.

Les instructions compressées réduisent la taille du programme et les lectures en mémoire. Le processeur les transforme en opérations internes ordinaires.

## Pourquoi 32 bits ?

Une adresse de 32 bits décrit un espace théorique de 4 Gio. Le MCU n'intègre qu'une petite partie de cette mémoire ; le reste sert à placer les périphériques et réserver des régions futures. Un cœur 64 bits élargirait registres, calculs et bus. Cette milestone privilégie un silicium embarqué maîtrisable.

## Zicsr, Zifencei et Machine mode

Les CSR contiennent l'état des interruptions, la cause d'une exception, les compteurs et la protection mémoire. **Zicsr** permet de les lire et modifier. **Zifencei** rend visible du code récemment écrit en mémoire.

Le Machine mode donne au firmware le contrôle complet. Linux demande habituellement le mode Supervisor et une MMU ; ces mécanismes appartiennent au futur processeur applicatif.

## ABI, ordre des octets et alignement

L'ABI **ilp32** définit notamment des entiers, des long et des pointeurs de 32 bits, ainsi que les règles d'appel entre fonctions.

Le mode little-endian range l'octet de poids faible à l'adresse la plus basse. La valeur 0x12345678 apparaît en mémoire sous la forme 78 56 34 12.

Un mot de 32 bits est naturellement placé à une adresse multiple de quatre. Notre contrat transforme un accès désaligné en exception claire. Le compilateur évite normalement ces accès.

## Logiciels visés

Le cœur devra exécuter des programmes bare-metal, newlib, FreeRTOS et Zephyr dans une configuration adaptée à la mémoire. Cette compatibilité sera démontrée par compilation, tests ISA et tests logiciels conservés après chaque modification.
