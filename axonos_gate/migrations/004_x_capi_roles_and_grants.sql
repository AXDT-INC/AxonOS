-- Version-four ownership and least-privilege grants. Run only after creating a
-- NOLOGIN owner and one distinct LOGIN worker role:
--   psql -X -v ON_ERROR_STOP=1 -v owner_role=axonos_x_capi_owner \
--     -v worker_role=axonos_x_capi_worker ... -f this-file
-- psql identifier variables are intentionally required; an omitted variable
-- is a syntax error under ON_ERROR_STOP instead of silently broadening access.
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';
-- pg_catalog is implicitly searched before public when it is not listed.
SET LOCAL search_path = public;

DO $schema$
BEGIN
    IF (SELECT schema_version FROM x_capi_schema_meta WHERE singleton=TRUE) <> 4 THEN
        RAISE EXCEPTION 'X CAPI grants require exact schema version 4';
    END IF;
END
$schema$;

-- Do not trust the mutable version row on an existing database.  This digest
-- covers the complete reviewed PG15 catalog shape: every x_capi table/index,
-- ordered column/type/default/storage property, constraint definition, index
-- definition/predicate, and any attached trigger, RLS policy, rewrite rule,
-- inheritance edge, or publication.  A grants-only run must fail before it
-- changes ownership if even one unreviewed object or weakened invariant exists.
DO $catalog_preflight$
DECLARE
    observed TEXT;
BEGIN
    IF current_setting('server_version_num')::INTEGER NOT BETWEEN 150000 AND 159999 THEN
        RAISE EXCEPTION 'X CAPI schema attestation requires pinned PostgreSQL 15';
    END IF;
    WITH target AS (
        SELECT c.relnamespace AS oid
          FROM pg_class c
         WHERE c.oid='x_capi_schema_meta'::regclass
    ), catalog_items(item) AS (
        SELECT jsonb_build_array(
                   'relation',c.relname,c.relkind,c.relpersistence,
                   c.relrowsecurity,c.relforcerowsecurity,c.relispartition,
                   c.relhasrules,c.relhastriggers,c.relreplident,c.reloftype,
                   COALESCE(am.amname,''),
                   COALESCE((SELECT array_agg(option ORDER BY option)
                               FROM unnest(c.reloptions) option),ARRAY[]::text[])
               )::text
          FROM pg_class c
          LEFT JOIN pg_am am ON am.oid=c.relam
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND left(c.relname,7)='x_capi_'
        UNION ALL
        SELECT jsonb_build_array(
                   'column',c.relname,a.attnum,a.attname,
                   format_type(a.atttypid,a.atttypmod),a.attnotnull,
                   COALESCE(pg_get_expr(d.adbin,d.adrelid),''),
                   a.attidentity,a.attgenerated,a.attstorage,
                   COALESCE(a.attcompression,''),
                   CASE WHEN a.attcollation=0 THEN '' ELSE
                        (SELECT n.nspname||'.'||coll.collname
                           FROM pg_collation coll
                           JOIN pg_namespace n ON n.oid=coll.collnamespace
                          WHERE coll.oid=a.attcollation) END
               )::text
          FROM pg_class c
          JOIN pg_attribute a ON a.attrelid=c.oid
          LEFT JOIN pg_attrdef d
            ON d.adrelid=a.attrelid AND d.adnum=a.attnum
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND c.relkind IN ('r','p') AND left(c.relname,7)='x_capi_'
           AND a.attnum>0 AND NOT a.attisdropped
        UNION ALL
        SELECT jsonb_build_array(
                   'dropped-column',c.relname,a.attnum
               )::text
          FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND c.relkind IN ('r','p') AND left(c.relname,7)='x_capi_'
           AND a.attnum>0 AND a.attisdropped
        UNION ALL
        SELECT jsonb_build_array(
                   'constraint',c.relname,con.conname,con.contype,
                   COALESCE(f.relname,''),pg_get_constraintdef(con.oid,false),
                   con.condeferrable,con.condeferred,con.convalidated,
                   con.connoinherit,con.confupdtype,con.confdeltype,
                   con.confmatchtype
               )::text
          FROM pg_constraint con
          JOIN pg_class c ON c.oid=con.conrelid
          LEFT JOIN pg_class f ON f.oid=con.confrelid
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND left(c.relname,7)='x_capi_'
        UNION ALL
        SELECT jsonb_build_array(
                   'index',t.relname,i.relname,am.amname,ix.indisunique,
                   ix.indisprimary,ix.indisexclusion,ix.indimmediate,
                   ix.indisvalid,ix.indisready,ix.indislive,ix.indisreplident,
                   ix.indnatts,ix.indnkeyatts,
                   ARRAY(SELECT pg_get_indexdef(ix.indexrelid,n,false)
                           FROM generate_series(1,ix.indnatts) n),
                   COALESCE(pg_get_expr(ix.indpred,ix.indrelid,false),''),
                   COALESCE((SELECT array_agg(option ORDER BY option)
                               FROM unnest(i.reloptions) option),ARRAY[]::text[])
               )::text
          FROM pg_index ix
          JOIN pg_class i ON i.oid=ix.indexrelid
          JOIN pg_class t ON t.oid=ix.indrelid
          JOIN pg_am am ON am.oid=i.relam
         WHERE t.relnamespace=(SELECT oid FROM target)
           AND left(t.relname,7)='x_capi_'
        UNION ALL
        SELECT jsonb_build_array(
                   'trigger',c.relname,t.tgname,t.tgenabled,
                   pg_get_triggerdef(t.oid,false)
               )::text
          FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND left(c.relname,7)='x_capi_' AND NOT t.tgisinternal
        UNION ALL
        SELECT jsonb_build_array(
                   'internal-trigger',c.relname,COALESCE(con.conname,''),
                   p.proname,t.tgtype,t.tgenabled,t.tgdeferrable,
                   t.tginitdeferred
               )::text
          FROM pg_trigger t
          JOIN pg_class c ON c.oid=t.tgrelid
          JOIN pg_proc p ON p.oid=t.tgfoid
          LEFT JOIN pg_constraint con ON con.oid=t.tgconstraint
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND left(c.relname,7)='x_capi_' AND t.tgisinternal
        UNION ALL
        SELECT jsonb_build_array(
                   'policy',c.relname,p.polname,p.polcmd,p.polpermissive,
                   p.polroles::text,
                   COALESCE(pg_get_expr(p.polqual,p.polrelid),''),
                   COALESCE(pg_get_expr(p.polwithcheck,p.polrelid),'')
               )::text
          FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND left(c.relname,7)='x_capi_'
        UNION ALL
        SELECT jsonb_build_array(
                   'rule',c.relname,r.rulename,r.ev_type,r.ev_enabled,
                   r.is_instead,pg_get_ruledef(r.oid,false)
               )::text
          FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND left(c.relname,7)='x_capi_'
        UNION ALL
        SELECT jsonb_build_array(
                   'inheritance',child_ns.nspname,child.relname,
                   parent_ns.nspname,parent.relname,i.inhseqno,
                   i.inhdetachpending
               )::text
          FROM pg_inherits i
          JOIN pg_class child ON child.oid=i.inhrelid
          JOIN pg_class parent ON parent.oid=i.inhparent
          JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
          JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
         WHERE (
               child.relnamespace=(SELECT oid FROM target)
               AND left(child.relname,7)='x_capi_'
           ) OR (
               parent.relnamespace=(SELECT oid FROM target)
               AND left(parent.relname,7)='x_capi_'
           )
        UNION ALL
        SELECT jsonb_build_array(
                   'publication',c.relname,p.pubname,
                   COALESCE(pg_get_expr(pr.prqual,pr.prrelid),''),
                   pr.prattrs::text
               )::text
          FROM pg_publication_rel pr
          JOIN pg_class c ON c.oid=pr.prrelid
          JOIN pg_publication p ON p.oid=pr.prpubid
         WHERE c.relnamespace=(SELECT oid FROM target)
           AND left(c.relname,7)='x_capi_'
        UNION ALL
        SELECT jsonb_build_array('publication-all-tables',p.pubname)::text
          FROM pg_publication p WHERE p.puballtables
    )
    SELECT md5(COALESCE(
        string_agg(item,E'\n' ORDER BY item COLLATE "C"),''
    )) INTO observed FROM catalog_items;
    IF observed <> 'ca1f629e8c751759fad46ca81a6fb561' THEN
        RAISE EXCEPTION
            'X CAPI exact-v4 catalog preflight mismatch; refuse grants-only migration';
    END IF;
END
$catalog_preflight$;

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
       OR has_database_privilege(worker_name,current_database(),'TEMPORARY')
       OR EXISTS (
        SELECT 1 FROM pg_database d
         WHERE d.datallowconn
             AND d.datname<>current_database()
             AND has_database_privilege(worker_name,d.oid,'CONNECT')
       ) THEN
        RAISE EXCEPTION 'X CAPI worker database boundary is not isolated';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_namespace n
         WHERE n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
           AND n.nspname<>'information_schema'
           AND has_schema_privilege(worker_name,n.oid,'CREATE')
    ) THEN
        RAISE EXCEPTION 'X CAPI worker schema boundary is not isolated';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
         WHERE p.prosecdef
           AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
           AND n.nspname<>'information_schema'
           AND has_function_privilege(worker_name,p.oid,'EXECUTE')
    ) OR EXISTS (
        SELECT 1 FROM pg_largeobject_metadata object
         WHERE object.lomowner=(SELECT oid FROM pg_roles WHERE rolname=worker_name)
            OR EXISTS (
               SELECT 1 FROM aclexplode(COALESCE(object.lomacl,'{}')) acl
                WHERE acl.grantee IN (
                    0,(SELECT oid FROM pg_roles WHERE rolname=worker_name)
                )
            )
    ) THEN
        RAISE EXCEPTION 'X CAPI worker indirect object privileges are not isolated';
    END IF;
END
$worker_database_boundary$;

DO $schema_usage$
DECLARE
    worker_name NAME;
BEGIN
    SELECT role_name INTO worker_name FROM x_capi_requested_roles
     WHERE role_kind='worker';
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I',
                   current_schema(), worker_name);
END
$schema_usage$;

ALTER TABLE x_capi_schema_meta OWNER TO :"owner_role";
ALTER TABLE x_capi_config_guard OWNER TO :"owner_role";
ALTER TABLE x_capi_attribution_contexts OWNER TO :"owner_role";
ALTER TABLE x_capi_revocation_tombstones OWNER TO :"owner_role";
ALTER TABLE x_capi_capacity OWNER TO :"owner_role";
ALTER TABLE x_capi_dedup OWNER TO :"owner_role";
ALTER TABLE x_capi_outbox OWNER TO :"owner_role";
ALTER TABLE x_capi_counters OWNER TO :"owner_role";
ALTER TABLE x_capi_worker_state OWNER TO :"owner_role";

-- Ownership transfer does not erase ACLs previously granted to other roles.
-- Read the ACL catalogs directly: information_schema hides grants whose
-- grantor/grantee is not an enabled role. Remove every explicit table- and
-- column-level grant before rebuilding the intended worker access below. Any
-- catalog or revoke failure aborts all ownership/grant changes.
DO $clear_acl$
DECLARE
    item RECORD;
    principal_sql TEXT;
BEGIN
    FOR item IN
        SELECT DISTINCT c.relname AS table_name,
               CASE WHEN acl.grantee=0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(acl.grantee) END AS grantee
          FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          CROSS JOIN LATERAL aclexplode(c.relacl) acl
         WHERE n.nspname=current_schema()
           AND c.relkind IN ('r','p')
           AND c.relname IN (
               'x_capi_schema_meta','x_capi_config_guard',
               'x_capi_attribution_contexts','x_capi_revocation_tombstones',
               'x_capi_capacity','x_capi_dedup','x_capi_outbox',
               'x_capi_counters','x_capi_worker_state'
           )
    LOOP
        principal_sql := CASE WHEN item.grantee='PUBLIC' THEN 'PUBLIC'
                              ELSE format('%I',item.grantee) END;
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM %s',
            current_schema(),item.table_name,principal_sql
        );
    END LOOP;
    FOR item IN
        SELECT DISTINCT c.relname AS table_name, a.attname AS column_name,
               CASE WHEN acl.grantee=0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(acl.grantee) END AS grantee
          FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_attribute a ON a.attrelid=c.oid
          CROSS JOIN LATERAL aclexplode(a.attacl) acl
         WHERE n.nspname=current_schema()
           AND c.relkind IN ('r','p')
           AND a.attnum>0 AND NOT a.attisdropped
           AND c.relname IN (
               'x_capi_schema_meta','x_capi_config_guard',
               'x_capi_attribution_contexts','x_capi_revocation_tombstones',
               'x_capi_capacity','x_capi_dedup','x_capi_outbox',
               'x_capi_counters','x_capi_worker_state'
           )
    LOOP
        principal_sql := CASE WHEN item.grantee='PUBLIC' THEN 'PUBLIC'
                              ELSE format('%I',item.grantee) END;
        EXECUTE format(
            'REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I FROM %s',
            item.column_name,current_schema(),item.table_name,principal_sql
        );
    END LOOP;
END
$clear_acl$;

REVOKE ALL ON x_capi_schema_meta, x_capi_config_guard,
    x_capi_attribution_contexts,
    x_capi_revocation_tombstones, x_capi_capacity,
    x_capi_outbox, x_capi_dedup, x_capi_counters,
    x_capi_worker_state FROM PUBLIC, :"worker_role";

-- The public gate talks to the worker over a credential-checked local socket.
-- Its normal database role deliberately has no access to any X CAPI object.

-- The isolated worker ingests authenticated local event envelopes, verifies
-- consent RPC capabilities, and owns delivery/retention. It can read only the
-- CSRF digest needed for that equality check, never the gate's core tables.
GRANT SELECT (singleton, schema_version) ON x_capi_schema_meta TO :"worker_role";
GRANT SELECT, INSERT, UPDATE ON x_capi_config_guard TO :"worker_role";
GRANT SELECT (id, handle_hash, csrf_hash, consent_state, mode_scope, policy_version,
    policy_epoch, lifecycle_expires_at, twclid, consented_at, expires_at,
    audience_scope, wallet_hash, wallet_bound_at,
    first_seen_at, updated_at)
    ON x_capi_attribution_contexts TO :"worker_role";
GRANT INSERT (id, handle_hash, csrf_hash, consent_state, mode_scope,
    policy_version, policy_epoch, lifecycle_expires_at, twclid, consented_at,
    audience_scope, expires_at, wallet_hash,
    wallet_bound_at, first_seen_at, updated_at)
    ON x_capi_attribution_contexts TO :"worker_role";
GRANT UPDATE (consent_state, twclid, declined_at, revoked_at, expires_at, wallet_hash,
    wallet_bound_at, updated_at)
    ON x_capi_attribution_contexts TO :"worker_role";
GRANT DELETE ON x_capi_attribution_contexts TO :"worker_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON x_capi_revocation_tombstones
    TO :"worker_role";
GRANT SELECT, UPDATE ON x_capi_capacity TO :"worker_role";
GRANT SELECT, INSERT, DELETE ON x_capi_dedup TO :"worker_role";
-- PostgreSQL requires some UPDATE privilege for SELECT ... FOR UPDATE even
-- though cleanup only deletes the locked row.
GRANT UPDATE (expires_at) ON x_capi_dedup TO :"worker_role";
GRANT SELECT (conversion_id, milestone, pixel_id, event_id,
    conversion_timestamp_ms, twclid, context_id, consent_policy_version,
    consent_policy_epoch, consent_audience_scope, attribution_expires_at,
    mode_scope, status, attempt_count,
    next_attempt_at, lease_owner, lease_token, lease_expires_at, created_at,
    updated_at, last_error_code)
    ON x_capi_outbox TO :"worker_role";
GRANT INSERT (conversion_id, milestone, source_key_hash, mode_scope, pixel_id,
    event_id, conversion_timestamp_ms, twclid, context_id,
    consent_policy_version, consent_policy_epoch, consent_audience_scope,
    attribution_expires_at, status, attempt_count,
    next_attempt_at, created_at, updated_at)
    ON x_capi_outbox TO :"worker_role";
GRANT UPDATE (status, attempt_count, next_attempt_at, lease_owner, lease_token,
    lease_expires_at, accepted_at, last_error_code, safe_debug_id, twclid,
    updated_at)
    ON x_capi_outbox TO :"worker_role";
GRANT DELETE ON x_capi_outbox TO :"worker_role";
GRANT SELECT, INSERT, UPDATE ON x_capi_counters TO :"worker_role";
GRANT SELECT, INSERT, UPDATE ON x_capi_worker_state TO :"worker_role";

-- Fail the migration if the rebuilt ACL is broader or narrower than the
-- reviewed worker contract.  Checking only that required grants exist would
-- allow a stale table-wide SELECT/UPDATE/TRUNCATE grant to survive unnoticed.
CREATE TEMPORARY TABLE x_capi_expected_table_acl (
    table_name NAME NOT NULL,
    privilege_type TEXT NOT NULL,
    is_grantable BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (table_name, privilege_type)
) ON COMMIT DROP;
INSERT INTO x_capi_expected_table_acl(table_name,privilege_type) VALUES
    ('x_capi_config_guard','SELECT'),
    ('x_capi_config_guard','INSERT'),
    ('x_capi_config_guard','UPDATE'),
    ('x_capi_attribution_contexts','DELETE'),
    ('x_capi_revocation_tombstones','SELECT'),
    ('x_capi_revocation_tombstones','INSERT'),
    ('x_capi_revocation_tombstones','UPDATE'),
    ('x_capi_revocation_tombstones','DELETE'),
    ('x_capi_capacity','SELECT'),
    ('x_capi_capacity','UPDATE'),
    ('x_capi_dedup','SELECT'),
    ('x_capi_dedup','INSERT'),
    ('x_capi_dedup','DELETE'),
    ('x_capi_outbox','DELETE'),
    ('x_capi_counters','SELECT'),
    ('x_capi_counters','INSERT'),
    ('x_capi_counters','UPDATE'),
    ('x_capi_worker_state','SELECT'),
    ('x_capi_worker_state','INSERT'),
    ('x_capi_worker_state','UPDATE');

CREATE TEMPORARY TABLE x_capi_expected_column_acl (
    table_name NAME NOT NULL,
    column_name NAME NOT NULL,
    privilege_type TEXT NOT NULL,
    is_grantable BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (table_name, column_name, privilege_type)
) ON COMMIT DROP;
INSERT INTO x_capi_expected_column_acl(
    table_name,column_name,privilege_type
)
    SELECT 'x_capi_schema_meta', column_name, 'SELECT'
      FROM unnest(ARRAY['singleton','schema_version']) column_name
UNION ALL
    SELECT 'x_capi_attribution_contexts', column_name, 'SELECT'
      FROM unnest(ARRAY[
        'id','handle_hash','csrf_hash','consent_state','mode_scope',
        'policy_version','policy_epoch','lifecycle_expires_at','twclid',
        'consented_at','expires_at','audience_scope','wallet_hash',
        'wallet_bound_at','first_seen_at','updated_at'
      ]) column_name
UNION ALL
    SELECT 'x_capi_attribution_contexts', column_name, 'INSERT'
      FROM unnest(ARRAY[
        'id','handle_hash','csrf_hash','consent_state','mode_scope',
        'policy_version','policy_epoch','lifecycle_expires_at','twclid',
        'consented_at','audience_scope','expires_at','wallet_hash',
        'wallet_bound_at','first_seen_at','updated_at'
      ]) column_name
UNION ALL
    SELECT 'x_capi_attribution_contexts', column_name, 'UPDATE'
      FROM unnest(ARRAY[
        'consent_state','twclid','declined_at','revoked_at','expires_at',
        'wallet_hash','wallet_bound_at','updated_at'
      ]) column_name
UNION ALL
    SELECT 'x_capi_dedup', 'expires_at', 'UPDATE'
UNION ALL
    SELECT 'x_capi_outbox', column_name, 'SELECT'
      FROM unnest(ARRAY[
        'conversion_id','milestone','pixel_id','event_id',
        'conversion_timestamp_ms','twclid','context_id',
        'consent_policy_version','consent_policy_epoch',
        'consent_audience_scope','attribution_expires_at','mode_scope',
        'status','attempt_count','next_attempt_at','lease_owner','lease_token',
        'lease_expires_at','created_at','updated_at','last_error_code'
      ]) column_name
UNION ALL
    SELECT 'x_capi_outbox', column_name, 'INSERT'
      FROM unnest(ARRAY[
        'conversion_id','milestone','source_key_hash','mode_scope','pixel_id',
        'event_id','conversion_timestamp_ms','twclid','context_id',
        'consent_policy_version','consent_policy_epoch',
        'consent_audience_scope','attribution_expires_at','status',
        'attempt_count','next_attempt_at','created_at','updated_at'
      ]) column_name
UNION ALL
    SELECT 'x_capi_outbox', column_name, 'UPDATE'
      FROM unnest(ARRAY[
        'status','attempt_count','next_attempt_at','lease_owner','lease_token',
        'lease_expires_at','accepted_at','last_error_code','safe_debug_id',
        'twclid','updated_at'
      ]) column_name;

DO $acl_postflight$
DECLARE
    worker_oid OID := (
        SELECT r.oid FROM pg_roles r JOIN x_capi_requested_roles q
          ON q.role_name=r.rolname WHERE q.role_kind='worker'
    );
    owner_oid OID := (
        SELECT r.oid FROM pg_roles r JOIN x_capi_requested_roles q
          ON q.role_name=r.rolname WHERE q.role_kind='owner'
    );
BEGIN
    IF (
        SELECT count(*)<>9 OR bool_or(c.relowner<>owner_oid)
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')
           AND c.relname IN (
             'x_capi_schema_meta','x_capi_config_guard',
             'x_capi_attribution_contexts','x_capi_revocation_tombstones',
             'x_capi_capacity','x_capi_dedup','x_capi_outbox',
             'x_capi_counters','x_capi_worker_state'
           )
    ) THEN
        RAISE EXCEPTION 'X CAPI relation owner postflight mismatch';
    END IF;
    IF EXISTS (
        WITH actual AS (
            SELECT c.relname::NAME AS table_name, acl.privilege_type,
                   acl.is_grantable
              FROM pg_class c
              JOIN pg_namespace n ON n.oid=c.relnamespace
              CROSS JOIN LATERAL aclexplode(c.relacl) acl
             WHERE n.nspname=current_schema() AND acl.grantee=worker_oid
               AND c.relname IN (
                 'x_capi_schema_meta','x_capi_config_guard',
                 'x_capi_attribution_contexts','x_capi_revocation_tombstones',
                 'x_capi_capacity','x_capi_dedup','x_capi_outbox',
                 'x_capi_counters','x_capi_worker_state'
               )
        ), mismatch AS (
            (SELECT * FROM actual EXCEPT SELECT * FROM x_capi_expected_table_acl)
            UNION ALL
            (SELECT * FROM x_capi_expected_table_acl EXCEPT SELECT * FROM actual)
        )
        SELECT 1 FROM mismatch
    ) THEN
        RAISE EXCEPTION 'X CAPI worker table ACL postflight mismatch';
    END IF;
    IF EXISTS (
        WITH actual AS (
            SELECT c.relname::NAME AS table_name, a.attname::NAME AS column_name,
                   acl.privilege_type,acl.is_grantable
              FROM pg_class c
              JOIN pg_namespace n ON n.oid=c.relnamespace
              JOIN pg_attribute a ON a.attrelid=c.oid
              CROSS JOIN LATERAL aclexplode(a.attacl) acl
             WHERE n.nspname=current_schema() AND acl.grantee=worker_oid
               AND a.attnum>0 AND NOT a.attisdropped
               AND c.relname IN (
                 'x_capi_schema_meta','x_capi_config_guard',
                 'x_capi_attribution_contexts','x_capi_revocation_tombstones',
                 'x_capi_capacity','x_capi_dedup','x_capi_outbox',
                 'x_capi_counters','x_capi_worker_state'
               )
        ), mismatch AS (
            (SELECT * FROM actual EXCEPT SELECT * FROM x_capi_expected_column_acl)
            UNION ALL
            (SELECT * FROM x_capi_expected_column_acl EXCEPT SELECT * FROM actual)
        )
        SELECT 1 FROM mismatch
    ) THEN
        RAISE EXCEPTION 'X CAPI worker column ACL postflight mismatch';
    END IF;
    IF EXISTS (
        SELECT 1
         FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          CROSS JOIN LATERAL aclexplode(c.relacl) acl
         WHERE n.nspname=current_schema()
           AND c.relname IN (
             'x_capi_schema_meta','x_capi_config_guard',
             'x_capi_attribution_contexts','x_capi_revocation_tombstones',
             'x_capi_capacity','x_capi_dedup','x_capi_outbox',
             'x_capi_counters','x_capi_worker_state'
           )
           AND acl.grantee NOT IN (owner_oid,worker_oid)
    ) OR EXISTS (
        SELECT 1
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_attribute a ON a.attrelid=c.oid
          CROSS JOIN LATERAL aclexplode(a.attacl) acl
         WHERE n.nspname=current_schema()
           AND c.relname IN (
             'x_capi_schema_meta','x_capi_config_guard',
             'x_capi_attribution_contexts','x_capi_revocation_tombstones',
             'x_capi_capacity','x_capi_dedup','x_capi_outbox',
             'x_capi_counters','x_capi_worker_state'
           )
           AND acl.grantee NOT IN (owner_oid,worker_oid)
    ) THEN
        RAISE EXCEPTION 'unexpected principal retains an X CAPI ACL';
    END IF;
END
$acl_postflight$;
COMMIT;
