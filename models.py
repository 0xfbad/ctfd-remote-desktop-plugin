from CTFd.models import db


class DesktopDockerContextModel(db.Model):
	__tablename__ = 'desktop_docker_contexts'
	id = db.Column(db.Integer, primary_key=True)
	context_name = db.Column(db.String(512), unique=True, nullable=False)
	hostname = db.Column(db.String(512), nullable=True)
	pub_hostname = db.Column(db.String(512), nullable=False)
	weight = db.Column(db.Integer, default=1)
	enabled = db.Column(db.Boolean, default=True)


class DesktopSettingsModel(db.Model):
	__tablename__ = 'desktop_settings'
	key = db.Column(db.String(512), primary_key=True)
	value = db.Column(db.Text)


SETTING_DEFAULTS = {
	'docker_image': 'ctfd-remote-desktop:latest',
	'memory_limit': '4g',
	'shm_size': '512m',
	'resolution': '1920x1080',
	'cpu_limit': '2',
	'initial_duration': '3600',
	'extension_duration': '1800',
	'max_extensions': '3',
	'vnc_ready_attempts': '180',
	'http_request_timeout': '3',
	'cleanup_interval': '300',
}


def get_setting(key, default=None):
	if default is None:
		default = SETTING_DEFAULTS.get(key)
	try:
		row = DesktopSettingsModel.query.filter_by(key=key).first()
		if row and row.value is not None:
			return row.value
	except Exception:
		pass
	return default


def set_setting(key, value):
	row = DesktopSettingsModel.query.filter_by(key=key).first()
	if row:
		row.value = str(value)
	else:
		row = DesktopSettingsModel(key=key, value=str(value))
		db.session.add(row)
	db.session.commit()


def get_all_settings():
	settings = dict(SETTING_DEFAULTS)
	try:
		rows = DesktopSettingsModel.query.all()
		for row in rows:
			settings[row.key] = row.value
	except Exception:
		pass
	return settings
