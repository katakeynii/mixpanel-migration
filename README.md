# Migration Mixpanel EU → EU : projet source vers projet vide

Deux projets dans **la même organisation**; résidence **EU**; historique **complet**.

## Limites importantes

Ce dépôt automatise seulement les **événements bruts**, les **profils utilisateurs** et les **profils de groupes** via `mixpanel-utils` 2.2.7. Il ne clone PAS un projet Mixpanel intégral. Boards, reports, cohortes, custom events/properties, lookup tables, Lexicon, réglages, accès, intégrations, gouvernance, flags et autres objets exigent un traitement complémentaire UI/API documenté, puis une vérification manuelle. La migration des alias/graphes d'identité n'est PAS garantie par la copie des événements; le système d'ID Merge et ses correspondances doivent être étudiés avant le transfert. Replay, historique des changements de profils et certaines métadonnées ne sont pas restaurés à l'identique.

## 0. Préparation (ne sautez pas ces étapes)

1. Vérifier la même région EU et le même **mode d'identité** (Original ID Merge / Simplified ID Merge), le fuseau horaire du projet source, ses options de propriété et sa rétention.
2. Dans le projet destination vide, recréer les **Group Keys** utilisées; relever source `data_group_id` et destination `data_group_id` dans Project Settings → Group Analytics.
3. Créer/autoriser les **service accounts** source et destination avec permissions nécessaires. Ne jamais commiter `.env` ou `backup/` (données potentiellement sensibles).
4. Inventorier Boards, Reports, Custom Events, Custom Properties, Cohorts, Lookup Tables, Lexicon, alertes, integrations, clés et permissions. Conserver captures/export des définitions. Vérifier le droit de déplacer les Boards dans Project Settings → Moving Boards.
5. Fixer `START_DATE` à la **date réelle du plus ancien événement exportable** (Mixpanel ne donne pas nécessairement un `from_date` automatique). `END_DATE` doit couvrir la première fenêtre de backfill. Exporter les dates de clôture ultérieures dans une seconde passe.
6. Choisir `TIMEZONE_OFFSET` à partir du **fuseau du projet source**. Ce paramètre est un décalage en **heures** attendu par `mixpanel-utils`, et non la région géographique `eu`; ne pas présumer que EU = +1. Tester sur deux journées encadrant un changement d'heure si le projet en utilise.
7. Pour de gros volumes, prévoir espace disque chiffré et quota/coût d'import, et tester une journée avant la reprise complète.

## 1. Configuration

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# modifier .env avec valeurs réelles
cp groups.example.json groups.json
# Si aucun Group Analytics, remplacer le contenu de groups.json par []
chmod 600 .env
```

Les secrets restent locaux. Ne pas placer le ZIP configuré avec des mots de passe dans un dépôt.

## 2. Test puis backfill

Pour premier test, choisir un court intervalle dans `.env` (1 jour avec des événements), puis :

```bash
python migrate.py all --dry-run
python migrate.py events
python migrate.py people
python migrate.py groups
python migrate.py status
```

Après validation, **préparer un nouveau répertoire BACKUP_DIR pour la migration complète** et restaurer `START_DATE/END_DATE` à la période totale. Puis :

```bash
python migrate.py all 2>&1 | tee migration.log
```

Chaque jour exporté est sauvegardé et validé en JSONL; une journée importée est marquée dans `backup/state.json` avec checksum SHA-256. Un redémarrage saute les journées terminées. La bibliothèque interne effectue ses propres lots/retries; le script ajoute jusqu'à quatre tentatives. **Une réponse partiellement acceptée par le serveur peut nécessiter une vérification ou une reprise**. Les événements ne sont pas nécessairement comptés un pour un à cause de la déduplication et de l'ID Merge. Ne supprimez jamais le projet d'origine sans validation approfondie.

**Attention :** un import de profils écrase potentiellement des propriétés existantes. Il est recommandé de traiter le delta juste avant la bascule, puis de réimporter les profils après les événements. `export_people` exporte l'état actuel des profils, pas l'historique de toutes les mises à jour.

## 3. Dépendances analytiques et Boards

1. Configurer d'abord la destination : mode identité, group keys, définitions de propriétés/événements personnalisés, tables lookup, cohortes, schéma Lexicon, permissions.
2. Les définitions avancées doivent être **recréées** si aucun export/import confirmé par votre offre/API. Le transfert d'un Board n'emporte pas automatiquement les objets dépendants.
3. Dupliquer chaque Board source puis déplacer la **copie** avec `... → Move to` vers la destination. Cela conserve un original dans la source. Tester la disponibilité de cette fonction et ses permissions.
4. Vérifier chaque Insights, Funnel, Retention, Board et segment par rapport aux métriques sources sur 1 j, 7 j, 30 j et mois historiques. Vérifier alertes et autres automatisations séparément.

## 4. Cutover recommandé

1. Backfill jusqu'à J-1. Effectuer une **courte phase de double tracking** ou préparer une bascule atomique du token de suivi dans toutes les applications et backends. Documenter l'instant précis du changement.
2. Exporter/importer le delta jusqu'à l'instant choisi (outil découpe par jours entiers, la déduplication des événements doit être assurée par `$insert_id`; voir section limites). Utiliser un `BACKUP_DIR` cohérent; une journée déjà marquée comme importée n'est pas resynchronisée automatiquement. Pour réexporter une journée qui a changé, traiter son statut avec une procédure contrôlée et sauvegardée, pas en modifiant aveuglément la base.
3. Re-synchroniser profils et groupes. Ne pas supposer que les alias et clusters de fusion se recréent tout seuls.
4. Comparer les volumes et les analyses dans la UI. Laisser la source en lecture seule pendant une période d'observation.
5. Retirer l'ancien token du code et des intégrations seulement après contrôle.

## 5. Checklist d'acceptation

- [ ] Même mode d'identity merge confirmé; cas distinct_id / device_id / user_id contrôlés
- [ ] Nombre d'events par jour/type comparé (en tenant compte des doublons)
- [ ] Propriétés et timestamps préservés sur échantillons
- [ ] Nombre de profils et propriétés clé comparés
- [ ] Groupes et group keys vérifiés
- [ ] Cohortes et custom definitions recréées et testées
- [ ] Board, Insights, Funnels, Retention comparés
- [ ] Lexicon, lookup tables, accès, alertes, intégrations, settings examinés
- [ ] Tracking en production routé vers la nouvelle destination EU
- [ ] Aucune erreur d'import non investiguée
- [ ] Ancien projet conservé pour rollback

## Références

- https://github.com/mixpanel/mixpanel-utils
- https://developer.mixpanel.com/reference/import-events
- https://community.mixpanel.com/x/ask-ai/msg_GGlEV3uBC9Mv/how-to-move-board-from-one-project-to-another


## Note sur les versions

Le code cible la version `mixpanel-utils==2.2.7`, dernière version disponible sur PyPI au 22 octobre 2026. Le dépôt GitHub annonce 3.0.0 mais cette version ne peut pas être installée via PyPI à cette date. Vérifier les méthodes sur une migration pilote avant tout import global.
