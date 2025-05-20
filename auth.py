from flask_security import Security, SQLAlchemyUserDatastore
from flask_security.forms import RegisterForm
from wtforms import StringField
from wtforms.validators import DataRequired, Length
from models import db, User, Role

# Custom registration form: collect username + email
class ExtendedRegisterForm(RegisterForm):
    username = StringField("Username", validators=[
        DataRequired(),
        Length(min=3, max=30, message="Username must be between 3 and 30 characters")
    ])

# Setup Flask-Security
user_datastore = SQLAlchemyUserDatastore(db, User, Role)
security = Security()

def init_security(app):
    # Register only the custom registration form
    app.config['SECURITY_REGISTER_FORM'] = ExtendedRegisterForm
    # Do NOT override SECURITY_USER_IDENTITY_ATTRIBUTES — default is email
    security.init_app(app, user_datastore)
    return app

