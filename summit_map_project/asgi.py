"""asgi entry point. not used in the docker setup but django makes it"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'summit_map_project.settings')

application = get_asgi_application()
