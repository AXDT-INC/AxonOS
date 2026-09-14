-- Read-only role preflight. The runner executes this before any fresh schema
-- install or ownership/ACL reconciliation.
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';
-- Omitting pg_catalog makes PostgreSQL search it implicitly before public, so
-- a pre-existing public routine cannot shadow security-sensitive built-ins.
SET LOCAL search_path = public;

CREATE TEMPORARY TABLE x_capi_requested_roles (
    role_kind TEXT PRIMARY KEY,
    role_name NAME NOT NULL
) ON COMMIT DROP;
INSERT INTO x_capi_requested_roles(role_kind,role_name) VALUES
    ('owner', :'owner_role'),
    ('worker', :'worker_role');

DO $roles$
BEGIN
    IF (SELECT count(DISTINCT role_name) FROM x_capi_requested_roles) <> 2 THEN
        RAISE EXCEPTION 'X CAPI owner and worker roles must be distinct';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles r JOIN x_capi_requested_roles q
          ON q.role_name=r.rolname
         WHERE q.role_kind='owner' AND NOT r.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'X CAPI owner role must exist and be NOLOGIN';
    END IF;
    IF (SELECT count(*) FROM pg_roles r JOIN x_capi_requested_roles q
          ON q.role_name=r.rolname
         WHERE q.role_kind='worker' AND r.rolcanlogin) <> 1 THEN
        RAISE EXCEPTION 'X CAPI worker role must exist and be LOGIN';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles r JOIN x_capi_requested_roles q
          ON q.role_name=r.rolname
         WHERE r.rolsuper OR r.rolbypassrls OR r.rolcreaterole
            OR r.rolcreatedb OR r.rolreplication
    ) THEN
        RAISE EXCEPTION
            'X CAPI runtime/owner roles must not have superuser, BYPASSRLS, CREATEROLE, CREATEDB, or replication privileges';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM x_capi_requested_roles q
          JOIN pg_roles inherited
            ON inherited.rolname <> q.role_name
           AND pg_has_role(q.role_name, inherited.rolname, 'MEMBER')
    ) THEN
        RAISE EXCEPTION
            'X CAPI runtime/owner roles must not inherit or SET ROLE to any other database role';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM x_capi_requested_roles q
          JOIN pg_roles target ON target.rolname=q.role_name
          JOIN pg_auth_members membership ON membership.roleid=target.oid
          JOIN pg_roles member_role ON member_role.oid=membership.member
         WHERE member_role.rolname<>target.rolname
    ) THEN
        RAISE EXCEPTION
            'No other database role may inherit or SET ROLE to an X CAPI role';
    END IF;
END
$roles$;

DO $worker_database_boundary$
DECLARE
    worker_name NAME;
BEGIN
    SELECT role_name INTO worker_name FROM x_capi_requested_roles
     WHERE role_kind='worker';
    IF has_database_privilege(worker_name,current_database(),'CREATE')
       OR has_database_privilege(worker_name,current_database(),'TEMPORARY') THEN
        RAISE EXCEPTION
            'X CAPI worker must not inherit CREATE or TEMPORARY on its dedicated database';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_database d
         WHERE d.datallowconn
           AND d.datname<>current_database()
           AND has_database_privilege(worker_name,d.oid,'CONNECT')
    ) THEN
        RAISE EXCEPTION
            'X CAPI worker must not CONNECT to any other database';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_namespace n
         WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
           AND n.nspname<>'information_schema'
           AND has_schema_privilege(worker_name,n.oid,'CREATE')
    ) THEN
        RAISE EXCEPTION
            'X CAPI worker must not create objects in any user schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
         WHERE p.prosecdef
           AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
           AND n.nspname<>'information_schema'
           AND has_function_privilege(worker_name,p.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION
            'X CAPI worker must not execute non-system SECURITY DEFINER routines';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_largeobject_metadata object
         WHERE object.lomowner=(SELECT oid FROM pg_roles WHERE rolname=worker_name)
            OR EXISTS (
               SELECT 1 FROM aclexplode(COALESCE(object.lomacl,'{}')) acl
                WHERE acl.grantee IN (
                    0,(SELECT oid FROM pg_roles WHERE rolname=worker_name)
                )
            )
    ) THEN
        RAISE EXCEPTION
            'X CAPI worker must not access database large objects';
    END IF;
END
$worker_database_boundary$;

-- A delegated grant chain may not be revocable after 004 transfers ownership.
-- Reject it before any ownership change; owner-issued ACLs are cleared
-- transactionally by 004 and then rebuilt from an exact allowlist.
DO $acl_grantors$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,'{}')) acl
         WHERE n.nspname=current_schema()
           AND c.relname IN (
               'x_capi_schema_meta','x_capi_config_guard',
               'x_capi_attribution_contexts','x_capi_revocation_tombstones',
               'x_capi_capacity','x_capi_dedup','x_capi_outbox',
               'x_capi_counters','x_capi_worker_state'
           )
           AND acl.grantor<>c.relowner
    ) OR EXISTS (
        SELECT 1
          FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_attribute a ON a.attrelid=c.oid
          CROSS JOIN LATERAL aclexplode(COALESCE(a.attacl,'{}')) acl
         WHERE n.nspname=current_schema()
           AND c.relname IN (
               'x_capi_schema_meta','x_capi_config_guard',
               'x_capi_attribution_contexts','x_capi_revocation_tombstones',
               'x_capi_capacity','x_capi_dedup','x_capi_outbox',
               'x_capi_counters','x_capi_worker_state'
           )
           AND a.attnum>0 AND NOT a.attisdropped
           AND acl.grantor<>c.relowner
    ) THEN
        RAISE EXCEPTION
            'X CAPI preflight refuses delegated table or column grant chains';
    END IF;
END
$acl_grantors$;

ROLLBACK;
