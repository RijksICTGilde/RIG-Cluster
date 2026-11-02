-- Aantallen uit de database `keycloak`, per realm. De telling van `task keycloak-telling`:
-- draai hem voor en na een versiesprong en vergelijk de twee met `diff`.
--
-- ALLEEN AANTALLEN. Geen e-mailadres, geen gebruikersnaam, geen id, geen hash, geen
-- secret_data: de uitvoer mag in een PR of een ticket belanden. Elke kolom in een SELECT
-- hieronder is daarom een aggregaat of een groeperingssleutel, en een toets in
-- `operations-manager/python/tests/test_keycloak_telling.py` weigert er iets anders.
--
-- De sleutelkolom draagt de uitsplitsing waar die er is (credentialtype, identity
-- provider, required action) en is leeg waar het om een enkel getal gaat.
SELECT * FROM (
    SELECT '(alle)' AS realm, 'realms' AS onderdeel, '' AS sleutel, COUNT(*) AS aantal
    FROM realm

    UNION ALL
    SELECT r.name, 'clients', '', COUNT(*)
    FROM client c JOIN realm r ON r.id = c.realm_id
    GROUP BY r.name

    UNION ALL
    SELECT r.name, 'users', '', COUNT(*)
    FROM user_entity u JOIN realm r ON r.id = u.realm_id
    GROUP BY r.name

    UNION ALL
    SELECT r.name, 'users_enabled', '', COUNT(*)
    FROM user_entity u JOIN realm r ON r.id = u.realm_id
    WHERE u.enabled
    GROUP BY r.name

    -- credential, user_role_mapping en user_required_action dragen geen realm_id, dus de
    -- realm komt via de gebruiker.
    UNION ALL
    SELECT r.name, 'credentials', c.type, COUNT(*)
    FROM credential c
    JOIN user_entity u ON u.id = c.user_id
    JOIN realm r ON r.id = u.realm_id
    GROUP BY r.name, c.type

    UNION ALL
    SELECT r.name, 'federated_identity', f.identity_provider, COUNT(*)
    FROM federated_identity f JOIN realm r ON r.id = f.realm_id
    GROUP BY r.name, f.identity_provider

    UNION ALL
    SELECT r.name, 'identity_providers', '', COUNT(*)
    FROM identity_provider i JOIN realm r ON r.id = i.realm_id
    GROUP BY r.name

    UNION ALL
    SELECT r.name, 'groups', '', COUNT(*)
    FROM keycloak_group g JOIN realm r ON r.id = g.realm_id
    GROUP BY r.name

    UNION ALL
    SELECT r.name, 'user_role_mappings', '', COUNT(*)
    FROM user_role_mapping m
    JOIN user_entity u ON u.id = m.user_id
    JOIN realm r ON r.id = u.realm_id
    GROUP BY r.name

    UNION ALL
    SELECT r.name, 'required_actions', a.required_action, COUNT(*)
    FROM user_required_action a
    JOIN user_entity u ON u.id = a.user_id
    JOIN realm r ON r.id = u.realm_id
    GROUP BY r.name, a.required_action
) telling
ORDER BY realm, onderdeel, sleutel
