"""wsgi entry point, gunicorn uses this"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'summit_map_project.settings')

application = get_wsgi_application()
