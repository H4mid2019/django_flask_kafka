"""empty message

Revision ID: 3941a2daebba
Revises: e7097a949cd7
Create Date: 2021-11-20 23:21:26.146804

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '3941a2daebba'
down_revision = 'e7097a949cd7'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('post', sa.Column('related', postgresql.JSON(astext_type=sa.Text()), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('post', 'related')
    # ### end Alembic commands ###
