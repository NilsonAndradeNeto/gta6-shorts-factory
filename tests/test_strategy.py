from datetime import datetime, timedelta, timezone
import json

from factory.analytics.strategy import initialize, save, learn, context, monitor


def test_missing_metrics_and_cohorts(db):
    initialize(db)
    published = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    for channel in ('own', 'other'):
        for i, views in enumerate((500, 2000, 100000)):
            save(db, 'youtube', channel, 'own' if channel == 'own' else 'competitor',
                 dict(id=channel+str(i), title='Jogo '+str(i), published_at=published,
                      views=views, duration=40, format='short_candidate', source='test'))
    findings = learn(db)['findings']
    assert len(findings) == 2
    assert findings[0]['median_views'] == 2000
    assert findings[0]['best']['views'] == 100000
    assert findings[0]['best']['likes'] is None
    assert findings[0]['best']['views_per_hour_recent'] is None
    assert '100000' in context(db)
    assert '100000' not in context(db, 'long')


def test_monitor_unavailable_and_interval(db, monkeypatch):
    monkeypatch.setattr('factory.analytics.strategy.yt.configured', lambda: False)
    monkeypatch.delenv('YOUTUBE_API_KEY', raising=False)
    monkeypatch.delenv('TIKTOK_ACCESS_TOKEN', raising=False)
    result = monitor(db)
    assert 'Configure' in result['youtube']
    assert 'indisponível' in result['tiktok']
    assert result['cohorts'] == 0
    assert monitor(db)['status'] == 'interval_not_due'
    assert db.one('SELECT COUNT(*) n FROM observations')['n'] == 0


def test_excludes_new_and_old_videos(db):
    initialize(db)
    for i in range(3):
        save(db, 'youtube', 'own', 'own', dict(id=str(i), title='novo', views=100000,
             published_at=datetime.now(timezone.utc).isoformat(), format='long', source='test'))
    assert learn(db)['findings'] == []
