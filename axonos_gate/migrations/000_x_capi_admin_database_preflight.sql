-- Prove that each stock administrative database is unused before bootstrap
-- changes its PUBLIC CONNECT ACL.  The caller permits only postgres/template1.
BEGIN READ ONLY;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';
SET LOCAL idle_in_transaction_session_timeout = '15s';
SET LOCAL search_path = public;

SELECT set_config('axonos_x_capi.bootstrap_role', :'bootstrap_role', TRUE);

DO $admin_database_preflight$
DECLARE
    bootstrap_name NAME :=
        current_setting('axonos_x_capi.bootstrap_role')::NAME;
BEGIN
    IF current_setting('server_version_num')::INTEGER
           NOT BETWEEN 150000 AND 159999
       OR pg_is_in_recovery()
       OR current_user<>bootstrap_name
       OR current_database() NOT IN ('postgres','template1') THEN
        RAISE EXCEPTION
            'X CAPI administrative-database preflight identity mismatch';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_namespace n
         WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
           AND n.nspname NOT IN ('information_schema','public')
    ) OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname='public'
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
    ) THEN
        RAISE EXCEPTION
            'X CAPI bootstrap refuses a used administrative database';
    END IF;
END
$admin_database_preflight$;

ROLLBACK;
