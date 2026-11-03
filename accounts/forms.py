from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.files.uploadedfile import UploadedFile

User = get_user_model()

MAX_AVATAR_BYTES = 5 * 1024 * 1024  # backup limit, the page shrinks first


def image_kind(upload):
    """work out if its a jpg png or gif by the first bytes"""
    head = upload.read(8)
    try:
        upload.seek(0)
    except Exception:
        pass
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


class ProfileForm(forms.ModelForm):
    username = forms.CharField(
        max_length=150, validators=[UnicodeUsernameValidator()],
        help_text="Letters, digits and @/./+/-/_ only.",
        widget=forms.TextInput(attrs={"autocomplete": "username"}),
    )
    avatar = forms.FileField(
        required=False, help_text="JPG, PNG or GIF.",
        # normal input, the remove photo button handles deleting
        widget=forms.FileInput)
    # set by the remove photo button, never shown on its own
    remove_avatar = forms.BooleanField(
        required=False, widget=forms.HiddenInput)

    class Meta:
        model = User
        fields = ("username", "avatar")

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.exclude(pk=self.instance.pk).filter(username=username).exists():
            raise forms.ValidationError("That username is taken.")
        return username

    def clean_avatar(self):
        avatar = self.cleaned_data.get("avatar")
        # only check new uploads. the old file can be missing from disk
        # and checking it would 500 the save
        if not avatar or not isinstance(avatar, UploadedFile):
            return avatar
        if avatar.size > MAX_AVATAR_BYTES:
            raise forms.ValidationError("Image must be under 5 MB.")
        if image_kind(avatar) is None:
            raise forms.ValidationError("Upload a JPG, PNG or GIF image.")
        return avatar

    def save(self, commit=True):
        # delete the old avatar if its being replaced or removed. a new
        # upload always wins over the remove button
        old = None
        if self.instance.pk:
            old = User.objects.filter(pk=self.instance.pk).values_list(
                "avatar", flat=True).first()
        user = super().save(commit=False)
        # with no new upload the field holds the old file and is truthy,
        # so only a real upload should beat the checkbox
        fresh_upload = isinstance(
            self.cleaned_data.get("avatar"), UploadedFile)
        if self.cleaned_data.get("remove_avatar") and not fresh_upload:
            user.avatar = None
        if commit:
            user.save()
        new = user.avatar.name if user.avatar else None
        if old and old != new and user.avatar.storage.exists(old):
            # file might already be gone if the volume was wiped, dont
            # error on that
            user.avatar.storage.delete(old)
        return user


class DeleteAccountForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput(
        attrs={"autocomplete": "current-password"}))
    confirm = forms.BooleanField(
        required=True,
        label="Yes, permanently delete my account and all my logs.")

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_password(self):
        password = self.cleaned_data["password"]
        if not self.user.check_password(password):
            raise forms.ValidationError("Wrong password.")
        return password
