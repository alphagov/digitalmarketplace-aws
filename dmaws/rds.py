import time
from datetime import datetime

import boto.rds
import boto.ec2
import psycopg2
import bcrypt

from . import utils


class RDS(object):
    def __init__(self, region, logger=None, profile_name=None):
        self.conn = boto.rds.connect_to_region(
            region,
            profile_name=profile_name)
        self.ec2conn = boto.ec2.connect_to_region(
            region,
            profile_name=profile_name)
        self.log = logger or (lambda *args, **kwargs: None)

    def get_instance(self, url=None, id=None):
        for instance in self.conn.get_all_dbinstances():
            if url and instance.endpoint[0] == url.split(':')[0]:
                return instance
            if id and instance.id == id:
                return instance

    def create_new_snapshot(self, snapshot_id, instance_id):
        """Create a new RDS snapshot deleting the existing snapshot if there is one
        """
        try:
            self.conn.get_all_dbsnapshots(snapshot_id)
            self.delete_snapshot(snapshot_id)
        except boto.exception.BotoServerError as e:
            if e.status != 404:
                raise

        snapshot = self.conn.create_dbsnapshot(snapshot_id, instance_id)

        self._wait_for_available(snapshot, "snapshot", "creating")

    def delete_snapshot(self, snapshot_id):
        self.conn.delete_dbsnapshot(snapshot_id)

    def create_new_security_group(self, name, dev_user_ips, vpc_id):
        self.delete_security_group(self.get_security_group(name))
        security_group = self.ec2conn.create_security_group(
            name, 'Allow access to the exportdata RDS instance',
            vpc_id=vpc_id)
        for dev_user_ip in dev_user_ips:
            self.ec2conn.authorize_security_group(
                ip_protocol='tcp', from_port=5432, to_port=5432,
                cidr_ip=dev_user_ip, group_id=security_group.id)

        return security_group

    def get_security_group(self, name):
        # The ec2 module does not raise helpful errors, everything is an EC2ResponseError
        # making it hard to tell if the security group didn't exist or there was some other
        # problem
        return next(
            (group for group in self.ec2conn.get_all_security_groups()
             if group.name == name), None)

    def delete_security_group(self, security_group):
        if security_group is not None:
            self.ec2conn.delete_security_group(security_group.name)

    def allow_access_to_instance(self, instance, security_group_name, dev_user_ips, vpc_id):
        self.log("Allow access {} {}".format(instance.id, security_group_name))
        security_group = self.get_security_group(security_group_name)
        if security_group:
            self.log("  > Found SG: {}:{}".format(security_group.id, security_group.name))
            instance = self.revoke_access_to_instance(instance, security_group)
            self.delete_security_group(security_group)

        security_group = self.create_new_security_group(
            security_group_name, dev_user_ips, vpc_id)

        security_group_ids = [
            sg.vpc_group for sg in instance.vpc_security_groups
        ] + [security_group.id]

        self.log("  > Adding {} to {}".format(security_group.id, [
            sg.vpc_group for sg in instance.vpc_security_groups
        ]))

        instance = self.conn.modify_dbinstance(
            instance.id, vpc_security_groups=security_group_ids, apply_immediately=True)

        self._wait_for_unavailable(instance, "RDS instance", "adding security group")
        self._wait_for_available(instance, "RDS instance", "adding security group")

        return instance

    def revoke_access_to_instance(self, instance, security_group):
        self.log("Revoking {} from {}".format(
            security_group.id,
            [sg.vpc_group for sg in instance.vpc_security_groups]))
        if security_group.id not in [sg.vpc_group for sg in instance.vpc_security_groups]:
            return instance
        security_group_ids = [
            sg.vpc_group for sg in instance.vpc_security_groups
            if sg.vpc_group != security_group.id
        ]
        if not security_group_ids:
            security_group_ids = [
                sg.id for sg in self.ec2conn.get_all_security_groups("default")
            ]

        self.log("  > updating {} security groups to {}".format(
            instance.id, security_group_ids))

        instance = self.conn.modify_dbinstance(
            instance.id, vpc_security_groups=security_group_ids, apply_immediately=True)

        self._wait_for_unavailable(instance, "RDS instance", "removing security group")
        self._wait_for_available(instance, "RDS instance", "removing security group")

        return instance

    def restore_instance_from_snapshot(self, snapshot_id, instance_id, dev_user_ips, vpc_id):
        instance = self.conn.restore_dbinstance_from_dbsnapshot(
            snapshot_id, instance_id,
            "db.t2.micro",
            multi_az=False)

        self._wait_for_available(instance, "RDS instance", "creating")

        instance = self.allow_access_to_instance(
            instance, "exportdata-dev-access",
            dev_user_ips, vpc_id)

        return instance

    def delete_instance(self, instance_id):
        instance = self.conn.delete_dbinstance(instance_id, skip_final_snapshot=True)
        self._wait_for_delete(instance, "RDS instance")

    def _wait_for_available(self, target, name, action, sleep=5):
        self.log(
            "Waiting for {} {} to be available after {}".format(
                name, target.id, action))
        while target.status != "available":
            self.log("  | {}".format(target.status))
            time.sleep(sleep)
            target.update(True)

    def _wait_for_unavailable(self, target, name, action, sleep=1, tries=20):
        self.log(
            "Waiting for {} {} to be unavailable after {}".format(
                name, target.id, action))
        while target.status == "available" and tries > 0:
            self.log("  | {}".format(target.status))
            time.sleep(sleep)
            tries -= 1
            target.update(True)

    def _wait_for_delete(self, target, name):
        try:
            while target.status == "deleting":
                self.log("Waiting while deleting {}".format(name))
                time.sleep(20)
                target.update()
        except boto.exception.BotoServerError as e:
            if e.status != 404:
                raise


class RDSPostgresClient(object):
    @staticmethod
    def from_boto(instance, database, user, password, logger=None):
        return RDSPostgresClient(
            instance.endpoint[0], instance.endpoint[1],
            database, user, password, logger)

    @staticmethod
    def from_url(url, user, password, logger=None):
        host, database = url.split('/')
        host, port = host.split(':')
        return RDSPostgresClient(host, port, database, user, password, logger)

    def __init__(self, host, port, database, user, password, logger=None):
        self.db_params = dict(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
        )
        self._cursor = None

        self.log = logger or (lambda *args, **kwargs: None)

    @property
    def cursor(self):
        if not self._cursor:
            self._connection = psycopg2.connect(**self.db_params)
            self._cursor = self._connection.cursor()

        return self._cursor

    @property
    def db_path(self):
        return "postgres://{user}:{password}@{host}:{port}/{database}".format(**self.db_params)

    def execute(self, sql):
        return self.cursor.execute(sql)

    def commit(self):
        self._connection.commit()

    def close(self):
        if self._cursor:
            self._cursor.close()
            self._connection.close()

    def dump(self, output_path):
        utils.run_cmd([
            'pg_dump', '-cb', '-d', self.db_path, '-f', output_path
        ], logger=self.log, ignore_errors=True)

    def load(self, export_path):
        utils.run_cmd([
            'psql', '-d', self.db_path, 'f', export_path
        ], logger=self.log, ignore_errors=True)

    def dump_to(self, target_pg_client):
        utils.run_piped_cmds([
            ['pg_dump', '-cb', '-d', self.db_path],
            ['psql', '-d', target_pg_client.db_path],
        ], logger=self.log, ignore_errors=False)

    def get_open_framework_ids(self):
        self.cursor.execute("SELECT id FROM frameworks WHERE status IN ('open', 'pending')")
        return tuple(framework[0] for framework in self.cursor.fetchall())

    def clean_database_for_staging(self):
        self.log("Update users")
        hashed_password = bcrypt.hashpw(
            b"Password1234",
            bcrypt.gensalt(4)
        ).decode('utf-8')
        self.cursor.execute(
            "UPDATE users SET name = 'Test user', email_address = id || '-user@example.com', password = '{}'"
            .format(hashed_password)
        )

        open_framework_ids = self.get_open_framework_ids()
        self.log("Delete draft services")
        self.cursor.execute("DELETE FROM draft_services")
        self.log("Delete supplier frameworks")
        if len(open_framework_ids):
            self.cursor.execute(
                "DELETE FROM supplier_frameworks WHERE framework_id IN %s",
                (open_framework_ids,))

        # Fix suppliers with services but no supplier_framework
        self.cursor.execute("""
            INSERT INTO supplier_frameworks (supplier_id, framework_id) (
                SELECT DISTINCT suppliers.supplier_id, services.framework_id
                FROM suppliers
                JOIN services ON suppliers.supplier_id=services.supplier_id
                WHERE (
                    SELECT COUNT(*) FROM supplier_frameworks WHERE supplier_id=suppliers.supplier_id
                ) = 0
            )
        """)

        self.log("Delete dangling suppliers")
        self.cursor.execute("""
            WITH dangling_suppliers AS (
                    -- Suppliers that are not connected to any frameworks
                    SELECT supplier_id FROM suppliers
                    WHERE (
                        SELECT COUNT(*) FROM supplier_frameworks WHERE supplier_id=suppliers.supplier_id
                    ) = 0
                ), d1 AS (
                    DELETE FROM contact_information WHERE supplier_id IN (SELECT supplier_id FROM dangling_suppliers)
                ), d2 AS (
                    DELETE FROM users WHERE supplier_id IN (SELECT supplier_id FROM dangling_suppliers)
                )
             DELETE FROM suppliers WHERE supplier_id IN (SELECT supplier_id FROM dangling_suppliers)
        """)

        self.log("Delete audit events")
        self.cursor.execute("DELETE FROM audit_events")

        self.log("Blank out declarations")
        self.cursor.execute("""
            UPDATE supplier_frameworks
            SET declaration = (CASE
                WHEN (declaration->'status') IS NULL
                THEN '{}'
                ELSE '{"status": "' || (declaration->>'status') || '"}'
            END)::json
            WHERE declaration IS NOT NULL AND declaration::varchar != 'null';
        """)

        self.commit()

    def clean_database_for_preview(self):
        hashed_password = bcrypt.hashpw(
            b"Password1234",
            bcrypt.gensalt(4)
        ).decode('utf-8')

        self.log("Create 1 supplier user per supplier")
        self.cursor.execute("DELETE FROM users")
        self.cursor.execute("""
            INSERT INTO users (
                name, email_address, password, active,
                created_at, updated_at, password_changed_at,
                role, supplier_id, failed_login_count
            ) (
                SELECT 'Test supplier user', 'supplier-' || supplier_id || '@example.com', %(password)s, 't',
                    %(timestamp)s, %(timestamp)s, %(timestamp)s,
                    'supplier', supplier_id, 0
                FROM suppliers
            )
        """, {
            'password': hashed_password,
            'timestamp': datetime.now()
        })
        self.log("Create 1 admin user per role")
        self.cursor.execute("""
            INSERT INTO users (
                name, email_address, password, active,
                created_at, updated_at, password_changed_at,
                role, failed_login_count
            ) (
                SELECT 'Test admin user', pg_enum.enumlabel || '@example.com', %(password)s, 't',
                    %(timestamp)s, %(timestamp)s, %(timestamp)s,
                    pg_enum.enumlabel::user_roles_enum, 0
                FROM pg_type
                JOIN pg_enum ON pg_type.oid = pg_enum.enumtypid
                WHERE pg_type.typname = 'user_roles_enum' AND pg_enum.enumlabel LIKE 'admin%%'
            )
        """, {
            'password': hashed_password,
            'timestamp': datetime.now()
        })

        self.commit()