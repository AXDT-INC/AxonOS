-- Read-only attestation for the Compose bootstrap superuser.  This runs before
-- role creation, password rotation, database ACL changes, or schema migration.
-- It deliberately accepts only a stock, dedicated PostgreSQL 15 cluster whose
-- sole application database is the requested X CAPI database.
BEGIN READ ONLY;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';
SET LOCAL idle_in_transaction_session_timeout = '15s';
-- pg_catalog is implicitly searched before public when it is omitted.
SET LOCAL search_path = public;

SELECT set_config('axonos_x_capi.bootstrap_role', :'bootstrap_role', TRUE),
       set_config('axonos_x_capi.database_name', :'database_name', TRUE),
       set_config('axonos_x_capi.owner_role', :'owner_role', TRUE),
       set_config('axonos_x_capi.worker_role', :'worker_role', TRUE);

DO $bootstrap_target_preflight$
DECLARE
    bootstrap_name NAME :=
        current_setting('axonos_x_capi.bootstrap_role')::NAME;
    database_name NAME :=
        current_setting('axonos_x_capi.database_name')::NAME;
    owner_name NAME := current_setting('axonos_x_capi.owner_role')::NAME;
    worker_name NAME := current_setting('axonos_x_capi.worker_role')::NAME;
    database_comment TEXT;
    database_owner NAME;
    application_role_count INTEGER;
BEGIN
    IF current_setting('server_version_num')::INTEGER
           NOT BETWEEN 150000 AND 159999 OR pg_is_in_recovery() THEN
        RAISE EXCEPTION
            'X CAPI bootstrap requires a writable pinned PostgreSQL 15 server';
    END IF;
    IF current_user<>bootstrap_name OR current_database()<>database_name THEN
        RAISE EXCEPTION
            'X CAPI bootstrap connection identity does not match configuration';
    END IF;
    IF database_name IN ('postgres','template0','template1') THEN
        RAISE EXCEPTION
            'X CAPI bootstrap requires a distinct application database';
    END IF;

    SELECT pg_get_userbyid(d.datdba),shobj_description(d.oid,'pg_database')
      INTO database_owner,database_comment
      FROM pg_database d
     WHERE d.datname=current_database() AND d.datallowconn
       AND NOT d.datistemplate;
    IF database_owner IS DISTINCT FROM bootstrap_name THEN
        RAISE EXCEPTION
            'X CAPI bootstrap role must own the requested application database';
    END IF;
    IF database_comment IS NOT NULL
       AND database_comment<>'AxonOS dedicated X CAPI database v1' THEN
        RAISE EXCEPTION
            'X CAPI bootstrap refuses a database without its dedicated marker';
    END IF;

    -- A shared cluster is outside the reviewed trust boundary.  Check every
    -- database, including databases with connections administratively disabled,
    -- before changing any cluster-level role or database ACL.
    IF NOT EXISTS (
        SELECT 1 FROM pg_database
         WHERE datname='postgres' AND NOT datistemplate AND datallowconn
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_database
         WHERE datname='template1' AND datistemplate AND datallowconn
    ) OR EXISTS (
        SELECT 1 FROM pg_database
         WHERE datname NOT IN (
             database_name::TEXT,'postgres','template0','template1'
         )
    ) OR EXISTS (
        SELECT 1 FROM pg_database
         WHERE pg_get_userbyid(datdba)<>bootstrap_name
    ) THEN
        RAISE EXCEPTION
            'X CAPI bootstrap refuses a non-dedicated PostgreSQL cluster';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_roles r
         WHERE r.rolname=bootstrap_name AND r.rolsuper AND r.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'X CAPI bootstrap identity is not the expected superuser';
    END IF;

    -- The official image creates one non-system bootstrap role.  On a restart,
    -- the two exactly-labelled X CAPI roles may additionally exist.  Any other
    -- user-created role is evidence that this is a shared or repurposed cluster.
    IF EXISTS (
        SELECT 1 FROM pg_roles r
         WHERE r.rolname NOT LIKE 'pg\_%' ESCAPE '\'
           AND r.rolname NOT IN (bootstrap_name,owner_name,worker_name)
    ) THEN
        RAISE EXCEPTION
            'X CAPI bootstrap refuses a cluster containing unrelated roles';
    END IF;
    SELECT count(*) INTO application_role_count
      FROM pg_roles WHERE rolname IN (owner_name,worker_name);
    IF application_role_count NOT IN (0,2) THEN
        RAISE EXCEPTION
            'X CAPI bootstrap refuses a partial application-role installation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles r
         WHERE r.rolname=owner_name
           AND (
               r.rolcanlogin OR r.rolsuper OR r.rolbypassrls
               OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
               OR r.rolconnlimit<>-1 OR r.rolvaliduntil IS NOT NULL
               OR r.rolconfig IS NOT NULL
               OR shobj_description(r.oid,'pg_authid') IS DISTINCT FROM
                  'AxonOS X CAPI owner role v1'
           )
    ) OR EXISTS (
        SELECT 1 FROM pg_roles r
         WHERE r.rolname=worker_name
           AND (
               NOT r.rolcanlogin OR r.rolsuper OR r.rolbypassrls
               OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
               OR r.rolconnlimit<>-1 OR r.rolvaliduntil IS NOT NULL
               OR r.rolconfig IS NOT NULL
               OR shobj_description(r.oid,'pg_authid') IS DISTINCT FROM
                  'AxonOS X CAPI worker role v1'
           )
    ) THEN
        RAISE EXCEPTION 'X CAPI bootstrap refuses altered application roles';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_auth_members membership
         WHERE membership.roleid IN (
                   SELECT oid FROM pg_roles
                    WHERE rolname IN (owner_name,worker_name)
               )
            OR membership.member IN (
                   SELECT oid FROM pg_roles
                    WHERE rolname IN (owner_name,worker_name)
               )
    ) THEN
        RAISE EXCEPTION
            'X CAPI bootstrap refuses application-role memberships';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles worker CROSS JOIN pg_namespace n
         WHERE worker.rolname=worker_name
           AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
           AND n.nspname<>'information_schema'
           AND has_schema_privilege(worker.rolname,n.oid,'CREATE')
    ) THEN
        RAISE EXCEPTION
            'X CAPI bootstrap refuses worker schema-creation privileges';
    END IF;

    -- The dedicated store has no unrelated schemas, persistent relations,
    -- routines, extensions, publications, foreign servers, or large objects.
    -- Exact fresh/v4 X CAPI catalog attestation follows in the bootstrap script.
    IF EXISTS (
        SELECT 1 FROM pg_namespace n
         WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
           AND n.nspname NOT IN ('information_schema','public')
    ) OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname='public' AND left(c.relname,7)<>'x_capi_'
    ) OR EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
         WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
           AND n.nspname<>'information_schema'
    ) OR EXISTS (
        SELECT 1 FROM pg_extension WHERE extname<>'plpgsql'
    ) OR EXISTS (
        SELECT 1 FROM pg_publication
    ) OR EXISTS (
        SELECT 1 FROM pg_subscription
    ) OR EXISTS (
        SELECT 1 FROM pg_foreign_server
    ) OR EXISTS (
        SELECT 1 FROM pg_largeobject_metadata
    ) OR EXISTS (
        SELECT 1 FROM pg_default_acl
    ) OR EXISTS (
        SELECT 1 FROM pg_db_role_setting
    ) THEN
        RAISE EXCEPTION
            'X CAPI bootstrap refuses unrelated objects or persistent defaults';
    END IF;
END
$bootstrap_target_preflight$;

ROLLBACK;
