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

# Circuit bands: each tag covers two grades (the ends narrow to one,
# project covers V8+). Mystery can be any grade — its true grade stays
# hidden until the setters reveal it the following week.
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
    """Grade labels one tag may represent (mystery allows any grade)."""
    return list(GRADES_BY_TAG.get(tag, []))


def tag_supports_grade(tag, grade):
    """True when a climb may wear `tag` at `grade`."""
    return grade in GRADES_BY_TAG.get(tag, [])


def grade_number(grade):
    """'V10' -> 10. Returns None for unparseable grades."""
    try:
        return int(str(grade).lstrip('Vv'))
    except (TypeError, ValueError):
        return None


class ClimbSet(models.Model):
    """One setting cycle for one wall. Only one set per wall is active at a
    time; leaderboards are scored per set so old boards stay viewable."""
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
        """Set label without the wall-name prefix (labels used to be
        stored as 'Main Wall — September 2026')."""
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
        """Grade shown to climbers: mystery climbs hide their grade
        behind '?' until the setters reveal it the following week."""
        if self.tag == 'mystery':
            return '?'
        return self.grade

    @property
    def is_mystery(self):
        return self.tag == 'mystery'

    @property
    def tag_class(self):
        """CSS class for the climb's circuit tag colour. The tag is a
        per-climb attribute, independent of grade: a V3 can wear red
        or green depending on how it was set."""
        if self.tag not in TAG_VALUES:
            return 'tag-mystery'
        return f'tag-{self.tag}'


class Ascent(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ascents')
    climb = models.ForeignKey(Climb, on_delete=models.CASCADE, related_name='ascents')
    tries = models.PositiveIntegerField()
    points = models.PositiveIntegerField(blank=True, null=True)
    logged_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.points:
            from .scoring import calculate_points
            self.points = calculate_points(
                self.climb.grade, self.tries, self.climb.tag)
        super().save(*args, **kwargs)

    class Meta:
        unique_together = ('user', 'climb')


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
    """Gym news (upcoming set dates, events...). Newest first;
    staff post from the news page itself."""
    title = models.CharField(max_length=200)
    body = models.TextField(max_length=5000)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='news_posts',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class Comment(models.Model):
    climb = models.ForeignKey(Climb, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='comments')
    text = models.TextField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.user} on {self.climb}: {self.text[:40]}"