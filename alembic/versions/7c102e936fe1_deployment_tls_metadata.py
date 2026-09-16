"""Store TLS metadata and certificate coverage; disambiguate deployment history.

Revision ID: 7c102e936fe1
Revises: 4756f3345db0
"""
import re
from alembic import op
import sqlalchemy as sa

revision = '7c102e936fe1'
down_revision = '4756f3345db0'
branch_labels = None
depends_on = None


def upgrade():
  with op.batch_alter_table('domain_deployments') as batch:
    batch.alter_column('status', existing_type=sa.Enum('DRAFT', 'APPLIED', 'ERROR', native_enum=False),
               type_=sa.Enum('DRAFT', 'APPLIED', 'ERROR', 'SUPERSEDED', native_enum=False), existing_nullable=False)
  op.add_column('domain_deployments', sa.Column('ssl_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
  op.add_column('domain_deployments', sa.Column('cert_name', sa.String(255), nullable=True))
  op.add_column('domain_certificates', sa.Column('server_names', sa.JSON(), nullable=True))
  connection = op.get_bind()
  deployments = sa.table('domain_deployments', sa.column('id'), sa.column('domain_id'),
               sa.column('config_text'), sa.column('ssl_enabled'),
               sa.column('cert_name'), sa.column('status'))
  for row in connection.execute(sa.select(deployments)).mappings().all():
    tls = bool(re.search(r'\blisten\s+[^;]*\bssl\b', row['config_text']))
    certificate = re.search(r'/live/([^/]+)/fullchain\.pem', row['config_text'])
    connection.execute(deployments.update().where(deployments.c.id == row['id']).values(
      ssl_enabled=tls, cert_name=certificate.group(1) if certificate else None))
  # Enum is a non-native VARCHAR, without a CHECK constraint in the existing schema.
  for domain_id in connection.execute(sa.select(deployments.c.domain_id).distinct()).scalars():
    ids = connection.execute(sa.select(deployments.c.id).where(
      deployments.c.domain_id == domain_id, deployments.c.status == 'APPLIED'
    ).order_by(deployments.c.id.desc())).scalars().all()
    if len(ids) > 1:
      connection.execute(deployments.update().where(deployments.c.id.in_(ids[1:])).values(status='SUPERSEDED'))


def downgrade():
  op.execute("UPDATE domain_deployments SET status='APPLIED' WHERE status='SUPERSEDED'")
  with op.batch_alter_table('domain_certificates') as batch:
    batch.drop_column('server_names')
  with op.batch_alter_table('domain_deployments') as batch:
    batch.alter_column('status', existing_type=sa.Enum('DRAFT', 'APPLIED', 'ERROR', 'SUPERSEDED', native_enum=False),
               type_=sa.Enum('DRAFT', 'APPLIED', 'ERROR', native_enum=False), existing_nullable=False)
    batch.drop_column('cert_name')
    batch.drop_column('ssl_enabled')
