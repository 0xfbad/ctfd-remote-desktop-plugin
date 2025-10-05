import datetime
import logging
import traceback
import json
import time
from flask import Blueprint, request, jsonify, render_template, Response, stream_with_context
from CTFd.utils.decorators import authed_only, admins_only
from CTFd.plugins import bypass_csrf_protection
from CTFd.utils.user import get_current_user
from .event_logger import event_logger

logger = logging.getLogger(__name__)

def _build_vnc_url(user_id):
	return f"/remote-desktop/vnc/{user_id}/vnc.html?autoconnect=1&resize=remote&path=websockify"

def create_routes(container_manager, orchestrator, config):
	remote_desktop_bp = Blueprint(
		'remote_desktop',
		__name__,
		template_folder='templates',
		static_folder='assets'
	)

	@remote_desktop_bp.route('/remote-desktop')
	@authed_only
	def remote_desktop_page():
		try:
			user = get_current_user()
			container_info = container_manager.get_container_info(user.id)

			vnc_url = ""
			formatted_time = ""
			if container_info:
				vnc_url = _build_vnc_url(user.id)
				created_timestamp = container_info['created_at']
				created_dt = datetime.datetime.fromtimestamp(created_timestamp)
				formatted_time = created_dt.strftime('%B %d, %Y at %I:%M %p')

			template_container_info = None
			if container_info:
				template_container_info = {
					'container_id': container_info['container_id'],
					'container_name': container_info['container_name'],
					'vnc_port': container_info['vnc_port'],
					'novnc_port': container_info['novnc_port'],
					'hostname': container_info['hostname'],
					'created_at': container_info['created_at']
				}

			return render_template(
				'remote_desktop.html',
				container_info=template_container_info,
				vnc_url=vnc_url,
				formatted_time=formatted_time
			)
		except Exception as e:
			logger.error(f"Error rendering remote desktop page: {str(e)}")
			logger.error(traceback.format_exc())
			return f"Error loading remote desktop page: {str(e)}", 500

	@remote_desktop_bp.route('/remote-desktop/api/status', methods=['GET'])
	@authed_only
	def get_status():
		try:
			user = get_current_user()
			container_info = container_manager.get_container_info(user.id)

			if not container_info:
				return jsonify({'session': None})

			timer_status = container_manager.get_session_timer_status(user.id)

			if timer_status.get('expired'):
				container_manager.destroy_container(user.id)
				return jsonify({'session': None})

			if timer_status.get('success') and not timer_status.get('started'):
				container_manager.start_session_timer(user.id)
				timer_status = container_manager.get_session_timer_status(user.id)

			vnc_url = _build_vnc_url(user.id)

			return jsonify({
				'session': {
					'created_at': container_info['created_at'],
					'vnc_url': vnc_url,
					'timer': {
						'active': timer_status.get('started', False),
						'time_remaining': timer_status.get('time_remaining', 0),
						'extensions_used': timer_status.get('extensions_used', 0),
						'max_extensions': timer_status.get('max_extensions', 3)
					} if timer_status.get('success') else None
				}
			})
		except Exception as e:
			logger.error(f"API error getting status: {str(e)}")
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/api/create', methods=['POST'])
	@authed_only
	@bypass_csrf_protection
	def create_session():
		try:
			user = get_current_user()
			logger.info(f"Create session request from user {user.name} (ID: {user.id})")

			if container_manager.get_container_info(user.id):
				event_logger.log_event(
					'session_error',
					'attempted to create session but already exists',
					user_id=user.id,
					username=user.name,
					level='warning'
				)
				return jsonify({'error': 'Session already exists'}), 400

			result = container_manager.create_container(user.id)

			if not result.get('success'):
				return jsonify({'error': result.get('error', 'Creation failed')}), 500

			return jsonify({
				'status': 'creating',
				'message': 'Container creation started'
			})
		except Exception as e:
			logger.error(f"API error creating session: {str(e)}", exc_info=True)
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/api/creation-status', methods=['GET'])
	@authed_only
	def get_creation_status():
		try:
			user = get_current_user()
			status = container_manager.get_creation_status(user.id)

			if not status:
				container_info = container_manager.get_container_info(user.id)
				if container_info:
					container_manager.start_session_timer(user.id)
					timer_status = container_manager.get_session_timer_status(user.id)
					vnc_url = _build_vnc_url(user.id)
					return jsonify({
						'status': 'ready',
						'message': 'Desktop ready!',
						'session': {
							'created_at': container_info['created_at'],
							'vnc_url': vnc_url,
							'timer': {
								'active': timer_status.get('started', False),
								'time_remaining': timer_status.get('time_remaining', 0),
								'extensions_used': timer_status.get('extensions_used', 0),
								'max_extensions': timer_status.get('max_extensions', 3)
							} if timer_status.get('success') else None
						}
					})
				return jsonify({'status': 'none'})

			if status.get('status') == 'ready':
				container_manager.start_session_timer(user.id)
				container_info = container_manager.get_container_info(user.id)
				timer_status = container_manager.get_session_timer_status(user.id)
				vnc_url = _build_vnc_url(user.id)

				return jsonify({
					'status': 'ready',
					'message': status.get('message', 'Desktop ready!'),
					'session': {
						'created_at': container_info['created_at'],
						'vnc_url': vnc_url,
						'timer': {
							'active': timer_status.get('started', False),
							'time_remaining': timer_status.get('time_remaining', 0),
							'extensions_used': timer_status.get('extensions_used', 0),
							'max_extensions': timer_status.get('max_extensions', 3)
						} if timer_status.get('success') else None
					}
				})

			return jsonify(status)
		except Exception as e:
			logger.error(f"API error getting creation status: {str(e)}")
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/api/destroy', methods=['POST'])
	@authed_only
	@bypass_csrf_protection
	def destroy_session():
		try:
			user = get_current_user()

			if not container_manager.get_container_info(user.id):
				return jsonify({'error': 'No active session'}), 400

			result = container_manager.destroy_container(user.id)
			if not result.get('success'):
				return jsonify({'error': result.get('error', 'Destruction failed')}), 500

			return jsonify({'session': None})
		except Exception as e:
			logger.error(f"API error destroying session: {str(e)}")
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/api/extend', methods=['POST'])
	@authed_only
	@bypass_csrf_protection
	def extend_session():
		try:
			user = get_current_user()

			if not container_manager.get_container_info(user.id):
				return jsonify({'error': 'No active session'}), 400

			result = container_manager.extend_session_timer(user.id)
			if not result.get('success'):
				return jsonify({'error': result.get('error', 'Extension failed')}), 400

			timer_status = container_manager.get_session_timer_status(user.id)
			return jsonify({
				'timer': {
					'active': timer_status.get('started', False),
					'time_remaining': timer_status.get('time_remaining', 0),
					'extensions_used': timer_status.get('extensions_used', 0),
					'max_extensions': timer_status.get('max_extensions', 3)
				}
			})
		except Exception as e:
			logger.error(f"API error extending session: {str(e)}")
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/api/cleanup', methods=['POST'])
	@authed_only
	@bypass_csrf_protection
	def trigger_cleanup():
		try:
			user = get_current_user()
			if not user.is_admin():
				return jsonify({'error': 'Admin access required'}), 403

			container_manager.periodic_cleanup()
			return jsonify({'success': True, 'message': 'Cleanup triggered'})
		except Exception as e:
			logger.error(f"API error triggering cleanup: {str(e)}")
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/admin')
	@admins_only
	def admin_dashboard():
		return render_template('admin_dashboard.html')

	@remote_desktop_bp.route('/remote-desktop/admin/api/containers', methods=['GET'])
	@admins_only
	@bypass_csrf_protection
	def admin_get_containers():
		try:
			container_manager.periodic_cleanup()
			containers = container_manager.get_all_containers()

			return jsonify({
				'containers': containers
			})
		except Exception as e:
			logger.error(f"Admin API error getting containers: {str(e)}")
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/admin/api/kill', methods=['POST'])
	@admins_only
	@bypass_csrf_protection
	def admin_kill_container():
		try:
			admin_user = get_current_user()
			user_id = request.form.get('user_id')
			if not user_id:
				return jsonify({'error': 'user_id required'}), 400

			user_id = int(user_id)

			from CTFd.models import Users
			target_user = Users.query.filter_by(id=user_id).first()
			target_username = target_user.name if target_user else f"User {user_id}"

			event_logger.log_event(
				'admin_action',
				f'admin {admin_user.name} manually killed session for {target_username}',
				user_id=user_id,
				username=target_username,
				level='warning',
				metadata={'admin_id': admin_user.id, 'admin_name': admin_user.name}
			)

			result = container_manager.destroy_container(user_id)

			if result.get('success'):
				logger.info(f"Admin killed container for user {user_id}")
				return jsonify({'success': True})
			else:
				return jsonify({'error': result.get('error', 'Failed to kill container')}), 500

		except Exception as e:
			logger.error(f"Admin API error killing container: {str(e)}")
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/api/auth-check', methods=['GET'])
	@authed_only
	def auth_check():
		try:
			current_user = get_current_user()
			user_id = request.headers.get('X-User-ID')

			if not user_id:
				return '', 400

			user_id = int(user_id)

			is_admin = hasattr(current_user, 'type') and current_user.type == 'admin'
			if not is_admin and current_user.id != user_id:
				logger.warning(f"User {current_user.id} attempted to access VNC for user {user_id}")
				return '', 403

			container_info = container_manager.get_container_info(user_id)
			if not container_info:
				return '', 404

			response = Response('', 200)
			response.headers['X-VNC-Host'] = container_info['hostname']
			response.headers['X-VNC-Port'] = str(container_info['novnc_port'])
			return response

		except Exception as e:
			logger.error(f"Auth check error: {str(e)}")
			return '', 500

	@remote_desktop_bp.route('/remote-desktop/admin/api/extend', methods=['POST'])
	@admins_only
	@bypass_csrf_protection
	def admin_extend_session():
		try:
			admin_user = get_current_user()
			user_id = request.form.get('user_id')
			if not user_id:
				return jsonify({'error': 'user_id required'}), 400

			user_id = int(user_id)

			if not container_manager.get_container_info(user_id):
				return jsonify({'error': 'No active session for user'}), 400

			from CTFd.models import Users
			target_user = Users.query.filter_by(id=user_id).first()
			target_username = target_user.name if target_user else f"User {user_id}"

			event_logger.log_event(
				'admin_action',
				f'admin {admin_user.name} extended session for {target_username}',
				user_id=user_id,
				username=target_username,
				level='info',
				metadata={'admin_id': admin_user.id, 'admin_name': admin_user.name}
			)

			result = container_manager.extend_session_timer(user_id)

			if result.get('success'):
				logger.info(f"Admin extended session for user {user_id}")
				return jsonify({'success': True})
			else:
				return jsonify({'error': result.get('error', 'Failed to extend session')}), 400

		except Exception as e:
			logger.error(f"Admin API error extending session: {str(e)}")
			return jsonify({'error': str(e)}), 500

	@remote_desktop_bp.route('/remote-desktop/admin/api/events/stream')
	@admins_only
	def admin_events_stream():
		def event_stream():
			import queue
			event_queue = queue.Queue(maxsize=100)

			def event_listener(event):
				try:
					event_queue.put_nowait(event)
				except queue.Full:
					pass

			event_logger.add_listener(event_listener)

			try:
				recent_events = event_logger.get_recent_events(limit=50)
				for event in recent_events:
					yield f"data: {json.dumps(event)}\n\n"

				while True:
					try:
						event = event_queue.get(timeout=30)
						yield f"data: {json.dumps(event)}\n\n"
					except queue.Empty:
						yield f": keepalive\n\n"

			finally:
				event_logger.remove_listener(event_listener)

		return Response(
			stream_with_context(event_stream()),
			mimetype='text/event-stream',
			headers={
				'Cache-Control': 'no-cache',
				'X-Accel-Buffering': 'no',
				'Connection': 'keep-alive'
			}
		)

	@remote_desktop_bp.route('/remote-desktop/admin/api/events/recent')
	@admins_only
	def admin_get_recent_events():
		try:
			limit = request.args.get('limit', 100, type=int)
			events = event_logger.get_recent_events(limit=limit)
			return jsonify({'events': events})
		except Exception as e:
			logger.error(f"Error getting recent events: {str(e)}")
			return jsonify({'error': str(e)}), 500

	return remote_desktop_bp
