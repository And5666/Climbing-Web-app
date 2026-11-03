from django.conf import settings
from django.db import models
from django.db.models import Avg


WALL_CHOICES = [('main', 'Main Wall'), ('cave', 'The Cave')]
GRADE_CHOICES = [(f'V{i}', f'V{i}') for i in range(0, 11)]
TAG_CHOICES = [
    ('white', 'White'), ('black', 'Black'), ('red', 'Red'),
    ('green', 'Green'), ('blue', 'Blue'), ('yellow', 'Yellow'),
    ('orange', 'Orange'), ('project', 'Project'), ('mystery', 'Mystery'),
]
TAG_VALUES = [slug for slug, _ in TAG_CHOICES]

# each tag covers a couple of grades. the ends only get one, project
# covers V8 and up. mystery can be any grade and stays hidden for a week
TAG_GRADE_NUMBERS = {
    'white': (0, 1),
    'black': (1, 2),
    'red': (2, 3),
    'green': (3, 4),
    'blue': (4, 5),
    'yellow': (5, 6),
    'orange': (6, 7),
    'project': (8, 9, 10),
    'mystery': tuple(range(0, 11)),
}

# mystery climbs hide the grade for a week. if the grade sits between
# two tags it takes the lower one, so V4 becomes green not blue
MYSTERY_REVEAL_DAYS = 7

GRADES_BY_TAG = {
    tag: [f'V{n}' for n in numbers]
    for tag, numbers in TAG_GRADE_NUMBERS.items()
}

TAGS_BY_GRADE = {}
for _tag, _grades in GRADES_BY_TAG.items():
    if _tag == 'mystery':
        continue
    for _grade in _grades:
        TAGS_BY_GRADE.setdefault(_grade, []).append(_tag)


def grades_for_tag(tag):
    """the grades this tag can be"""
    return list(GRADES_BY_TAG.get(tag, []))


def tag_supports_grade(tag, grade):
    """true if this tag and grade pair is allowed"""
    return grade in GRADES_BY_TAG.get(tag, [])


def tag_for_grade(grade):
    """the tag a grade belongs to. grades on a border belong to two
    tags, we take the lower one. V4 goes green, V5 goes blue. none if
    the grade is unknown"""
    tags = TAGS_BY_GRADE.get(grade) or []
    return tags[0] if tags else None


def grade_number(grade):
    """V10 becomes 10. none if it cant be read"""
    try:
        return int(str(grade).lstrip('Vv'))
    except (TypeError, ValueError):
        return None


# markers use the overlay viewbox, 256 by 512. the x_percent names are
# old, its not a percent anymore. dont change this
WALL_MAP_WIDTH = 256
WALL_MAP_HEIGHT = 512
MARKER_RADIUS = 7


def clamp_to_wall(x, y, radius=MARKER_RADIUS):
    """keep a marker inside the wall. the whole circle stays in view so
    x sits between r and width minus r"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        x = WALL_MAP_WIDTH / 2
    try:
        y = float(y)
    except (TypeError, ValueError):
        y = WALL_MAP_HEIGHT / 2
    x = min(max(x, radius), WALL_MAP_WIDTH - radius)
    y = min(max(y, radius), WALL_MAP_HEIGHT - radius)
    return (x, y)


# tape colours, name and hex. we store the name for presets but old
# climbs can have a raw hex and those still work. pages show the name
TAPE_COLOURS = [
    ("white", "#f5f5f0"),
    ("pure white", "#ffffff"),
    ("cream", "#f3ead7"),
    ("light grey", "#ced4da"),
    ("grey", "#868e96"),
    ("dark grey", "#495057"),
    ("charcoal", "#343a40"),
    ("soft black", "#2b2b2b"),
    ("black", "#1a1a1a"),
    ("jet black", "#000000"),
    ("tan", "#d8b98a"),
    ("light brown", "#b07a45"),
    ("brown", "#8c5a2b"),
    ("dark brown", "#5c3a1e"),
    ("wood", "#a06a35"),
    ("light red", "#ff8787"),
    ("red", "#e03131"),
    ("dark red", "#a61e1e"),
    ("maroon", "#862e2e"),
    ("light orange", "#ffc078"),
    ("orange", "#f08c00"),
    ("bright orange", "#ff7a00"),
    ("dark orange", "#d9480f"),
    ("coral", "#ff6b5e"),
    ("light yellow", "#ffec99"),
    ("yellow", "#ffd43b"),
    ("gold", "#f0b429"),
    ("mustard", "#d9b62c"),
    ("lime", "#94d82d"),
    ("light green", "#8ce99a"),
    ("green", "#2f9e44"),
    ("dark green", "#1e6b32"),
    ("light teal", "#63e6be"),
    ("teal", "#0ca678"),
    ("dark teal", "#087f5b"),
    ("light blue", "#a5d8ff"),
    ("sky blue", "#4dabf7"),
    ("blue", "#1971c2"),
    ("navy", "#1e3a5f"),
    ("light cyan", "#99e9f2"),
    ("cyan", "#22b8cf"),
    ("dark cyan", "#0b7285"),
    ("light purple", "#d0bfff"),
    ("purple", "#7048b6"),
    ("dark purple", "#3f2a6e"),
    ("light pink", "#fcc2d7"),
    ("pink", "#f783ac"),
    ("magenta", "#d6336c"),
    ("dark pink", "#a61e4d"),
]

# groups for the admin colour picker
TAPE_FAMILIES = [
    ("White", ["white", "pure white", "cream"]),
    ("Grey", ["light grey", "grey", "dark grey", "charcoal"]),
    ("Black", ["soft black", "black", "jet black"]),
    ("Brown", ["tan", "light brown", "brown", "dark brown", "wood"]),
    ("Red", ["light red", "red", "dark red", "maroon"]),
    ("Orange", ["light orange", "orange", "bright orange", "dark orange",
                "coral"]),
    ("Yellow", ["light yellow", "yellow", "gold", "mustard"]),
    ("Green", ["lime", "light green", "green", "dark green"]),
    ("Teal", ["light teal", "teal", "dark teal"]),
    ("Blue", ["light blue", "sky blue", "blue", "navy"]),
    ("Cyan", ["light cyan", "cyan", "dark cyan"]),
    ("Purple", ["light purple", "purple", "dark purple"]),
    ("Pink", ["light pink", "pink", "magenta", "dark pink"]),
]

TAPE_COLOUR_HEX = {name: hex_value for name, hex_value in TAPE_COLOURS}
TAPE_COLOUR_NAMES = {hex_value.lower(): name
                     for name, hex_value in TAPE_COLOURS}
TAPE_COLOUR_NAMES.update({name.lower(): name for name, _ in TAPE_COLOURS})


def _hex_to_rgb(text):
    text = str(text).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError("not a hex colour")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def colour_display_name(value):
    """readable name for a colour, orange instead of #c9265f. known
    names and hexes match exactly, anything else picks the closest
    preset. values that are not a colour are left alone"""
    if value is None:
        return ""
    key = str(value).strip().lower()
    if key in TAPE_COLOUR_NAMES:
        return TAPE_COLOUR_NAMES[key]
    try:
        r, g, b = _hex_to_rgb(key)
    except (ValueError, TypeError):
        return str(value)
    best, best_dist = TAPE_COLOURS[0][0], None
    for name, hex_value in TAPE_COLOURS:
        cr, cg, cb = _hex_to_rgb(hex_value)
        dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if best_dist is None or dist < best_dist:
            best, best_dist = name, dist
    return best


def colour_text_on(value):
    """black or white text for the marker, whichever shows up better on
    the tape colour"""
    hex_value = TAPE_COLOUR_HEX.get(str(value).strip().lower(), value)
    try:
        r, g, b = _hex_to_rgb(hex_value)
    except (ValueError, TypeError):
        return "#ffffff"
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000000" if luminance > 0.6 else "#ffffff"


class ClimbSet(models.Model):
    """one setting cycle on one wall. only one set per wall is live at a
    time and each set keeps its own leaderboard"""
    wall = models.CharField(max_length=10, choices=WALL_CHOICES)
    label = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.label} ({self.get_wall_display()})"

    @classmethod
    def active(cls, wall):
        return cls.objects.filter(wall=wall, is_active=True).first()

    @property
    def short_label(self):
        """label without the wall name. old labels had the wall name in
        front of the month"""
        prefix = f"{self.get_wall_display()} — "
        if self.label.startswith(prefix):
            return self.label[len(prefix):]
        return self.label


class Climb(models.Model):
    WALL_CHOICES = WALL_CHOICES
    GRADE_CHOICES = GRADE_CHOICES

    name = models.CharField(max_length=100, blank=True)
    colour = models.CharField(max_length=20)
    grade = models.CharField(max_length=5, choices=GRADE_CHOICES)
    tag = models.CharField(max_length=10, choices=TAG_CHOICES, default='white')
    wall = models.CharField(max_length=10, choices=WALL_CHOICES)
    climb_set = models.ForeignKey(
        ClimbSet, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='climbs',
    )
    x_percent = models.FloatField()
    y_percent = models.FloatField()
    date_set = models.DateField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name or self.get_grade_display()} ({self.colour}, {self.get_wall_display()})"

    @property
    def ascent_count(self):
        return self.ascents.count()

    @property
    def community_rating(self):
        return self.ratings.aggregate(avg=Avg('value'))['avg']

    @property
    def community_grade(self):
        from collections import Counter
        grades = self.grade_suggestions.values_list('suggested_grade', flat=True)
        if not grades:
            return None
        return Counter(grades).most_common(1)[0][0]

    @property
    def display_grade(self):
        """grade the climber sees. mystery climbs show a ? until the
        reveal"""
        if self.tag == 'mystery':
            return '?'
        return self.grade

    @property
    def is_mystery(self):
        return self.tag == 'mystery'

    @property
    def tag_class(self):
        """css class for the tag colour. tag comes from the climb not the
        grade, a V3 can be red or green"""
        if self.tag not in TAG_VALUES:
            return 'tag-mystery'
        return f'tag-{self.tag}'

    @property
    def colour_name(self):
        """readable tape name for boards, stats and logs"""
        return colour_display_name(self.colour)

    @property
    def colour_hex(self):
        """the hex to actually draw. names like sky blue are not valid
        css so they get looked up"""
        return TAPE_COLOUR_HEX.get(
            str(self.colour).strip().lower(), self.colour)


class Ascent(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ascents')
    climb = models.ForeignKey(Climb, on_delete=models.CASCADE, related_name='ascents')
    tries = models.PositiveIntegerField()
    points = models.PositiveIntegerField(blank=True, null=True)
    logged_at = models.DateTimeField(auto_now_add=True)
    # voided sends stay in the log but score nothing
    is_voided = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=280, blank=True, default="")

    def save(self, *args, **kwargs):
        if self.points is None:
            from .scoring import calculate_points
            # always use the setter grade, not the voted one, or people
            # could bump there own points
            self.points = calculate_points(
                self.climb.grade, self.tries, self.climb.tag)
        super().save(*args, **kwargs)

    class Meta:
        unique_together = ('user', 'climb')


class AscentAudit(models.Model):
    """one row for every send create, edit or delete. records the old
    and new values. times come from the server only"""
    ACTION_CHOICES = [
        ("create", "create"), ("edit", "edit"), ("delete", "delete"),
        ("void", "void"), ("unvoid", "unvoid"),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="ascent_audits")
    ascent = models.ForeignKey(
        Ascent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audits")
    climb = models.ForeignKey(
        Climb, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ascent_audits")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="ascent_audit_actions")
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    old_tries = models.PositiveIntegerField(null=True, blank=True)
    new_tries = models.PositiveIntegerField(null=True, blank=True)
    old_grade = models.CharField(max_length=5, blank=True, default="")
    new_grade = models.CharField(max_length=5, blank=True, default="")
    old_points = models.PositiveIntegerField(null=True, blank=True)
    new_points = models.PositiveIntegerField(null=True, blank=True)
    note = models.CharField(max_length=280, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} by {self.actor or self.user} ({self.created_at})"


class Flag(models.Model):
    """one anticheat finding. staff only, nothing happens automatic.
    every status change is done by staff with a note"""
    SEVERITIES = [("low", "low"), ("medium", "medium"), ("high", "high")]
    STATUSES = [("open", "open"), ("reviewed", "reviewed"),
                ("dismissed", "dismissed"), ("confirmed", "confirmed")]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="flags")
    ascent = models.ForeignKey(
        Ascent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="flags")
    rule_code = models.CharField(max_length=40)
    severity = models.CharField(max_length=10, choices=SEVERITIES)
    points = models.PositiveIntegerField(default=0)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=10, choices=STATUSES, default="open")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="reviewed_flags")
    note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.rule_code} on {self.user} ({self.severity})"


class ModerationLog(models.Model):
    """staff action log. flag reviews, voids, hides and removals go here"""
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name="moderation_actions")
    action = models.CharField(max_length=40)
    username = models.CharField(max_length=150, blank=True, default="")
    details = models.JSONField(default=dict)
    note = models.CharField(max_length=280, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.actor}: {self.action} {self.username}"


class GradeSuggestion(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='grade_suggestions')
    climb = models.ForeignKey(Climb, on_delete=models.CASCADE, related_name='grade_suggestions')
    suggested_grade = models.CharField(max_length=5, choices=Climb.GRADE_CHOICES)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'climb')


class Rating(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ratings')
    climb = models.ForeignKey(Climb, on_delete=models.CASCADE, related_name='ratings')
    value = models.PositiveSmallIntegerField()
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'climb')


class NewsPost(models.Model):
    """one forum post. the model keeps the old NewsPost name so the
    existing rows and migrations still line up. pinned posts sort
    above everything, then newest (or whichever order is picked)"""
    title = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField(max_length=5000)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='news_posts',
    )
    is_pinned = models.BooleanField(default=False)
    likes = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True,
        related_name='liked_forum_posts',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_pinned', '-created_at']

    def __str__(self):
        return self.title or self.body[:60]

    @property
    def author_is_staff(self):
        return bool(self.author and self.author.is_staff)


FORUM_COMMENT_MAX_LENGTH = 2000


class ForumComment(models.Model):
    """a comment on a forum post. replies point at their parent comment
    so the page can draw a reddit-style thread, oldest first. reply_to
    is kept for old mentions and is filled from the parent on new
    replies"""
    post = models.ForeignKey(
        NewsPost, on_delete=models.CASCADE, related_name='forum_comments')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='forum_comments')
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True,
        related_name='replies',
    )
    reply_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    text = models.TextField(max_length=FORUM_COMMENT_MAX_LENGTH)
    likes = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True,
        related_name='liked_forum_comments',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.user} on {self.post}: {self.text[:40]}"

    @property
    def user_is_staff(self):
        return bool(self.user and self.user.is_staff)


class Comment(models.Model):
    climb = models.ForeignKey(Climb, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='comments')
    text = models.TextField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.user} on {self.climb}: {self.text[:40]}"