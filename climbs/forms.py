from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm


class SignupForm(UserCreationForm):
    # signup only asks for username and email, password comes from the
    # normal django form
    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")