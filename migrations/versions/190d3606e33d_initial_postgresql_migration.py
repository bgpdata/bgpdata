"""Initial PostgreSQL Migration

Revision ID: 190d3606e33d
Revises: 
Create Date: 2024-10-19 01:44:21.526153

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '190d3606e33d'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Users Table
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False, unique=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
    )

    # RIS Table
    op.create_table(
        'ris',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('checkpoint', sa.String(255), nullable=False),
        sa.Column('timestamp', sa.DateTime, nullable=False),
        sa.Column('peer_ip', sa.String(50), nullable=False),
        sa.Column('peer_as', sa.Integer, nullable=False),
        sa.Column('host', sa.String(50), nullable=False),
        sa.Column('type', sa.String(1), nullable=False),  # A: Announcement, W: Withdrawal
        sa.Column('as_path', postgresql.ARRAY(sa.Integer), nullable=True),
        sa.Column('as_set', postgresql.ARRAY(sa.Integer), nullable=True),
        sa.Column('community', postgresql.ARRAY(sa.Integer, dimensions=2), nullable=True),
        sa.Column('origin', sa.String(50), nullable=True),
        sa.Column('med', sa.Integer, nullable=True),
        sa.Column('aggregator', sa.String(50), nullable=True),
        sa.Column('next_hop', postgresql.ARRAY(sa.String(50)), nullable=True),
        sa.Column('prefix', sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint('id', 'timestamp')
    )

    # Convert the table to a TimescaleDB hypertable
    op.execute("SELECT create_hypertable('ris', 'timestamp', if_not_exists => TRUE);")

    # Add a retention policy to automatically drop data older than 48 hours
    op.execute("SELECT add_retention_policy('ris', INTERVAL '24 hours');")

    # RIS Lite Table
    op.create_table(
        'ris_lite',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('timestamp', sa.DateTime, nullable=False),
        sa.Column('host', sa.String(50), nullable=False),
        sa.Column('prefix', sa.String(50), nullable=False),
        sa.Column('full_peer_count', sa.Integer, nullable=False),
        sa.Column('partial_peer_count', sa.Integer, nullable=False),
        sa.Column('segment', postgresql.ARRAY(sa.Integer), nullable=True),
        sa.PrimaryKeyConstraint('id', 'timestamp')
    )

    # Convert the table to a TimescaleDB hypertable
    op.execute("SELECT create_hypertable('ris_lite', 'timestamp', if_not_exists => TRUE);")

    # Add a retention policy to automatically drop data older than 48 hours
    op.execute("SELECT add_retention_policy('ris_lite', INTERVAL '24 hours');")

def downgrade() -> None:
    op.drop_table('users')
    op.drop_table('ris')
    op.drop_table('ris_lite')