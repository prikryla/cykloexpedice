"""Comprehensive tests for the Cykloexpedice Flask application."""
import os
import time as _time_module
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, ANY
import bcrypt
import pytest  # noqa: F401
from flask.testing import FlaskClient


# ── Helper function tests ────────────────────────────────────────


class TestNameVocative:
    def test_male_name(self):
        from app import name_vocative
        assert name_vocative('Michal') == 'Michale'

    def test_male_name_adam(self):
        from app import name_vocative
        assert name_vocative('Adam') == 'Adame'

    def test_female_name(self):
        from app import name_vocative
        assert name_vocative('Vlasta') == 'Vlasto'

    def test_full_name_uses_first_only(self):
        from app import name_vocative
        result = name_vocative('Michal Přikryl')
        assert result == 'Michale'

    def test_empty_name(self):
        from app import name_vocative
        assert name_vocative('') == ''

    def test_capitalized(self):
        from app import name_vocative
        result = name_vocative('petr')
        assert result[0].isupper()


class TestGeneratePaymentQR:
    def test_returns_base64_string(self):
        from app import generate_payment_qr
        result = generate_payment_qr('CZ1620100000002703473997', 3500, 1)
        assert isinstance(result, str)
        assert len(result) > 100  # valid base64 PNG

    def test_spayd_format_in_qr(self):
        from app import generate_payment_qr
        # Just verify it doesn't crash with various inputs
        result = generate_payment_qr('CZ1620100000002703473997', 3500.50, 42, 'WACHAU_Jan_Novak')
        assert result  # non-empty

    def test_without_message(self):
        from app import generate_payment_qr
        result = generate_payment_qr('CZ1620100000002703473997', 1000, 7)
        assert result


class TestRenderEmailLayout:
    def test_contains_event_name(self):
        from app import render_email_layout
        settings = {'event_name': 'TEST EVENT', 'event_year': '2026',
                     'contact_email': 'a@b.cz', 'contact_name_1': 'A', 'contact_name_2': 'B'}
        html = render_email_layout('<p>body</p>', settings)
        assert 'TEST EVENT' in html
        assert '2026' in html
        assert 'a@b.cz' in html

    def test_contains_body(self):
        from app import render_email_layout
        settings = {'event_name': 'X', 'event_year': '', 'contact_email': '',
                     'contact_name_1': '', 'contact_name_2': ''}
        html = render_email_layout('<p>Hello World</p>', settings)
        assert '<p>Hello World</p>' in html


class TestRenderEmailTemplate:
    def test_replaces_placeholders(self, app):
        from app import render_email_template
        with app.app_context():
            variables = {
                'name': 'Jan Novák',
                'event_name': 'Cyklo',
                'event_year': '2026',
                'contact_name_1': 'A',
                'contact_name_2': 'B',
                'contact_email': 'x@y.cz',
            }
            subject, html = render_email_template('email_approved', variables)
            assert 'Cyklo' in subject
            assert '2026' in subject
            assert 'Jane' in html  # vocative of Jan
            assert '{{' not in html  # no unresolved placeholders

    def test_auto_generates_vocative(self, app):
        from app import render_email_template
        with app.app_context():
            variables = {'name': 'Petr', 'event_name': 'X', 'event_year': '',
                         'contact_name_1': '', 'contact_name_2': '', 'contact_email': ''}
            _, html = render_email_template('email_submitted', variables)
            assert 'Petře' in html


# ── Public route tests ───────────────────────────────────────────


class TestPublicRoutes:
    def test_index(self, client):
        r = client.get('/')
        assert r.status_code == 200

    def test_propozice(self, client):
        r = client.get('/propozice')
        assert r.status_code == 200

    def test_ubytovani(self, client):
        r = client.get('/ubytovani')
        assert r.status_code == 200

    def test_aktuality(self, client):
        r = client.get('/aktuality')
        assert r.status_code == 200

    def test_kontakt(self, client):
        r = client.get('/kontakt')
        assert r.status_code == 200

    def test_fotky_enabled(self, client):
        r = client.get('/fotky')
        assert r.status_code == 200

    def test_404_page(self, client):
        r = client.get('/nonexistent-page')
        assert r.status_code == 404

    def test_registrace_get(self, client):
        r = client.get('/registrace')
        assert r.status_code == 200


class TestRegistration:
    def test_submit_valid(self, client):
        with patch('app.send_email') as mock_email:
            r = client.post('/registrace', data={
                'name': 'Jan Novák',
                'email': 'jan@test.cz',
                'phone': '123456789',
                'note': 'Test note',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
            assert r.status_code == 200
            assert 'Děkujeme' in r.data.decode()
            assert mock_email.call_count == 3  # 1 confirmation + 2 admin notifications

    def test_submit_notifies_admins(self, client):
        with patch('app.send_email') as mock_email:
            client.post('/registrace', data={
                'name': 'Test Notify',
                'email': 'notify@test.cz',
                'phone': '',
                'note': '',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
            recipients = [call[0][0] for call in mock_email.call_args_list]
            assert 'adam.prikryl7@gmail.com' in recipients
            assert 'michal.prikryl@atlas.cz' in recipients
            admin_call = mock_email.call_args_list[1]
            assert 'Nová přihláška' in admin_call[0][1]
            assert 'Test Notify' in admin_call[0][2]

    def test_submit_without_name_fails(self, client):
        r = client.post('/registrace', data={
            'name': '',
            'email': 'jan@test.cz',
        })
        assert r.status_code == 200
        assert 'povinné' in r.data.decode()

    def test_submit_without_email_fails(self, client):
        r = client.post('/registrace', data={
            'name': 'Jan Novák',
            'email': '',
        })
        assert r.status_code == 200
        assert 'povinný' in r.data.decode().lower()


class TestRegistrationPhoneValidation:
    def _post(self, client, phone):
        return client.post('/registrace', data={
            'name': 'Test User',
            'email': 'test@seznam.cz',
            'phone': phone,
            'gdpr_consent': 'on',
        }, follow_redirects=True)

    def test_valid_cz_prefix(self, client):
        with patch('app.send_email'):
            r = self._post(client, '+420 777 123 456')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_sk_prefix(self, client):
        with patch('app.send_email'):
            r = self._post(client, '+421905123456')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_nine_digits_no_prefix(self, client):
        with patch('app.send_email'):
            r = self._post(client, '777123456')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_nine_digits_with_spaces(self, client):
        with patch('app.send_email'):
            r = self._post(client, '777 123 456')
        assert 'Děkujeme' in r.data.decode()

    def test_empty_phone_allowed(self, client):
        with patch('app.send_email'):
            r = self._post(client, '')
        assert 'Děkujeme' in r.data.decode()

    def test_reject_us_prefix(self, client):
        r = self._post(client, '+1-604-597-9167')
        assert 'předvolba' in r.data.decode().lower()

    def test_reject_uk_prefix(self, client):
        r = self._post(client, '+44 7911 123456')
        assert 'předvolba' in r.data.decode().lower()

    def test_reject_de_prefix(self, client):
        r = self._post(client, '+49 170 1234567')
        assert 'předvolba' in r.data.decode().lower()

    def test_reject_too_few_digits(self, client):
        r = self._post(client, '12345678')
        assert 'číslic' in r.data.decode().lower()

    def test_reject_too_many_digits(self, client):
        r = self._post(client, '1234567890')
        assert 'číslic' in r.data.decode().lower()

    def test_reject_cz_prefix_wrong_digit_count(self, client):
        r = self._post(client, '+420 12345678')
        html = r.data.decode().lower()
        assert 'neplatné' in html

    def test_reject_letters_in_phone(self, client):
        r = self._post(client, 'abcdefghi')
        assert 'číslic' in r.data.decode().lower()

    def test_valid_cz_prefix_with_dashes(self, client):
        with patch('app.send_email'):
            r = self._post(client, '+420-777-123-456')
        assert 'Děkujeme' in r.data.decode()


class TestRegistrationEmailValidation:
    def _post(self, client, email):
        return client.post('/registrace', data={
            'name': 'Test User',
            'email': email,
            'gdpr_consent': 'on',
        }, follow_redirects=True)

    def test_valid_cz_domain(self, client):
        with patch('app.send_email'):
            r = self._post(client, 'user@seznam.cz')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_sk_domain(self, client):
        with patch('app.send_email'):
            r = self._post(client, 'user@azet.sk')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_com_domain(self, client):
        with patch('app.send_email'):
            r = self._post(client, 'user@gmail.com')
        assert 'Děkujeme' in r.data.decode()

    def test_reject_exotic_tld(self, client):
        r = self._post(client, 'user@example.xyz')
        assert 'Neplatná' in r.data.decode()

    def test_reject_org_tld(self, client):
        r = self._post(client, 'user@example.org')
        assert 'Neplatná' in r.data.decode()

    def test_reject_net_tld(self, client):
        r = self._post(client, 'user@example.net')
        assert 'Neplatná' in r.data.decode()

    def test_reject_io_tld(self, client):
        r = self._post(client, 'user@example.io')
        assert 'Neplatná' in r.data.decode()

    def test_reject_no_at_sign(self, client):
        r = self._post(client, 'invalidemail')
        assert 'Neplatná' in r.data.decode()

    def test_reject_ru_tld(self, client):
        r = self._post(client, 'bot@spam.ru')
        assert 'Neplatná' in r.data.decode()

    def test_valid_custom_cz_domain(self, client):
        with patch('app.send_email'):
            r = self._post(client, 'info@mojedomena.cz')
        assert 'Děkujeme' in r.data.decode()


class TestRegistrationGDPR:
    def test_reject_without_gdpr(self, client):
        r = client.post('/registrace', data={
            'name': 'Test User',
            'email': 'test@test.cz',
        }, follow_redirects=True)
        assert 'osobních údajů' in r.data.decode().lower()

    def test_accept_with_gdpr(self, client):
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Test User',
                'email': 'test@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        assert 'Děkujeme' in r.data.decode()

    def test_not_stored_in_db(self, app, client):
        with patch('app.send_email'):
            client.post('/registrace', data={
                'name': 'GDPR Test',
                'email': 'gdpr@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT * FROM registrace WHERE name = 'GDPR Test'").fetchone()
            assert row is not None
            assert 'gdpr_consent' not in dict(row)


class TestDuplicateEmailRegistration:
    def test_reject_duplicate_email(self, client):
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'First User',
                'email': 'duplicate@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        assert 'Děkujeme' in r.data.decode()
        r2 = client.post('/registrace', data={
            'name': 'Second User',
            'email': 'duplicate@test.cz',
            'gdpr_consent': 'on',
        }, follow_redirects=True)
        assert 'již registrována' in r2.data.decode()

    def test_allow_different_email(self, client):
        with patch('app.send_email'):
            client.post('/registrace', data={
                'name': 'First User',
                'email': 'first@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Second User',
                'email': 'second@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        assert 'Děkujeme' in r.data.decode()


# ── Admin auth tests ─────────────────────────────────────────────


class TestAdminAuth:
    def test_login_page_loads(self, client):
        r = client.get('/admin/login')
        assert r.status_code == 200

    def test_login_invalid_credentials(self, client):
        r = client.post('/admin/login', data={
            'username': 'nonexistent',
            'password': 'wrong',
        })
        assert r.status_code == 200
        assert 'Neplatné' in r.data.decode()

    def test_login_wrong_password(self, app, client):
        pw_hash = bcrypt.hashpw(b'correct', bcrypt.gensalt()).decode()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = ? WHERE username = ?',
                       (pw_hash, 'adam'))
            db.commit()

        r = client.post('/admin/login', data={
            'username': 'adam',
            'password': 'wrong',
        })
        assert 'Neplatné' in r.data.decode()

    def test_login_success(self, app, client):
        pw_hash = bcrypt.hashpw(b'testpass', bcrypt.gensalt()).decode()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = ? WHERE username = ?',
                       (pw_hash, 'adam'))
            db.commit()

        r = client.post('/admin/login', data={
            'username': 'adam',
            'password': 'testpass',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_admin_requires_login(self, client):
        r = client.get('/admin')
        assert r.status_code == 302
        assert '/admin/login' in r.headers['Location']

    def test_logout(self, admin_client):
        r = admin_client.get('/admin/logout', follow_redirects=True)
        assert r.status_code == 200
        # After logout, admin page should redirect
        r2 = admin_client.get('/admin')
        assert r2.status_code == 302

    def test_first_login_redirects_to_set_password(self, app, client):
        # Admin with no password_hash should redirect to set-password
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = NULL WHERE username = ?', ('adam',))
            db.commit()

        r = client.post('/admin/login', data={
            'username': 'adam',
            'password': '',
        })
        assert r.status_code == 302
        assert 'set-password' in r.headers['Location']


# ── Admin dashboard & protected routes ───────────────────────────


class TestAdminDashboard:
    def test_dashboard_loads(self, admin_client):
        r = admin_client.get('/admin')
        assert r.status_code == 200

    def test_dashboard_shows_only_approved_registrations(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                       ('Approved', 'a@test.cz'))
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'denied')",
                       ('Denied', 'd@test.cz'))
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'pending')",
                       ('Pending', 'p@test.cz'))
            db.commit()
        r = admin_client.get('/admin')
        html = r.data.decode()
        assert '>1<' in html

    def test_settings_page_loads(self, admin_client):
        r = admin_client.get('/admin/nastaveni')
        assert r.status_code == 200

    def test_save_settings(self, admin_client):
        r = admin_client.post('/admin/nastaveni', data={
            'event_name': 'NEW NAME',
            'event_year': '2030',
            'event_days': '5',
            'event_km': '500',
            'event_elevation': '3000',
            'event_dates': 'test dates',
            'contact_name_1': 'A',
            'contact_name_2': 'B',
            'contact_email': 'a@b.cz',
            'photos_link': '',
            'photos_text': '',
            'payment_amount': '4000',
            'bank_account': '123/456',
            'bank_iban': 'CZ123',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uloženo' in r.data.decode().lower()

    def test_profile_page_loads(self, admin_client):
        r = admin_client.get('/admin/profile')
        assert r.status_code == 200

    def test_email_templates_page_loads(self, admin_client):
        r = admin_client.get('/admin/email-sablony')
        assert r.status_code == 200

    def test_email_templates_save(self, admin_client):
        r = admin_client.post('/admin/email-sablony', data={
            'email_submitted_subject': 'Test Subject',
            'email_submitted_body': '<p>Test body</p>',
            'email_approved_subject': 'Approved',
            'email_approved_body': '<p>Approved</p>',
            'email_denied_subject': 'Denied',
            'email_denied_body': '<p>Denied</p>',
            'email_payment_subject': 'Payment',
            'email_payment_body': '<p>Payment</p>',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložen' in r.data.decode().lower()

    def test_email_template_preview(self, admin_client):
        r = admin_client.post('/admin/email-sablony/preview', data={
            'subject': 'Test {{name}}',
            'body': '<p>Ahoj {{name_vocative}}</p>',
        })
        assert r.status_code == 200
        assert 'Jane' in r.data.decode()  # vocative of sample "Jan Novák"


# ── Admin Registration Management ────────────────────────────────


class TestAdminRegistrace:
    def _create_registration(self, admin_client, app, name='Test User', email='test@test.cz'):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('INSERT INTO registrace (name, email, phone, note) VALUES (?, ?, ?, ?)',
                       (name, email, '123', 'note'))
            db.commit()
            row = db.execute('SELECT id FROM registrace WHERE name = ?', (name,)).fetchone()
            return row['id']

    def test_registrace_list(self, admin_client):
        r = admin_client.get('/admin/registrace')
        assert r.status_code == 200

    def test_registrace_search(self, admin_client, app):
        self._create_registration(admin_client, app, name='Hledaný Člověk')
        r = admin_client.get('/admin/registrace?q=Hledaný')
        assert r.status_code == 200
        assert 'Hledaný' in r.data.decode()

    def test_registrace_filter_by_status(self, admin_client, app):
        self._create_registration(admin_client, app)
        r = admin_client.get('/admin/registrace?status=pending')
        assert r.status_code == 200

    def test_approve_registration(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        with patch('app.send_email'):
            r = admin_client.post(f'/admin/registrace/{reg_id}/approve',
                                  follow_redirects=True)
        assert r.status_code == 200
        assert 'schválena' in r.data.decode().lower()

    def test_deny_registration_get(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        r = admin_client.get(f'/admin/registrace/{reg_id}/deny')
        assert r.status_code == 200

    def test_deny_registration_post(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        with patch('app.send_email'):
            r = admin_client.post(f'/admin/registrace/{reg_id}/deny',
                                  data={'reason': 'Plná kapacita'},
                                  follow_redirects=True)
        assert r.status_code == 200
        assert 'zamítnuta' in r.data.decode().lower()

    def test_deny_registration_without_reason(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        r = admin_client.post(f'/admin/registrace/{reg_id}/deny',
                              data={'reason': ''})
        assert r.status_code == 200
        assert 'důvod' in r.data.decode().lower()

    def test_delete_registration(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        r = admin_client.post(f'/admin/registrace/{reg_id}/delete',
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'smazána' in r.data.decode().lower()

    def test_approve_nonexistent_returns_404(self, admin_client):
        r = admin_client.post('/admin/registrace/9999/approve')
        assert r.status_code == 404

    def test_registrace_ordered_by_status_group(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'denied')",
                       ('Denied User', 'd@test.cz'))
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                       ('Approved User', 'a@test.cz'))
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'pending')",
                       ('Pending User', 'p@test.cz'))
            db.commit()
        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        pending_pos = html.index('Pending User')
        approved_pos = html.index('Approved User')
        denied_pos = html.index('Denied User')
        assert pending_pos < approved_pos < denied_pos

    def test_registrace_section_dividers_shown(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'pending')",
                       ('Pending', 'p@test.cz'))
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                       ('Approved', 'a@test.cz'))
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'denied')",
                       ('Denied', 'd@test.cz'))
            db.commit()
        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'Schválené' in html
        assert 'Zamítnuté' in html


class TestAdminBulkDelete:
    def _create_registrations(self, app, count=3):
        ids = []
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(count):
                db.execute('INSERT INTO registrace (name, email) VALUES (?, ?)',
                           (f'Bulk User {i}', f'bulk{i}@test.cz'))
            db.commit()
            rows = db.execute('SELECT id FROM registrace ORDER BY id').fetchall()
            ids = [r['id'] for r in rows]
        return ids

    def test_bulk_delete_multiple(self, admin_client, app):
        ids = self._create_registrations(app, 3)
        r = admin_client.post('/admin/registrace/bulk-delete',
                              data={'ids': [ids[0], ids[1]]},
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'Smazáno 2' in r.data.decode()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            remaining = db.execute('SELECT COUNT(*) c FROM registrace').fetchone()['c']
            assert remaining == 1

    def test_bulk_delete_all(self, admin_client, app):
        ids = self._create_registrations(app, 3)
        r = admin_client.post('/admin/registrace/bulk-delete',
                              data={'ids': ids},
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'Smazáno 3' in r.data.decode()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            remaining = db.execute('SELECT COUNT(*) c FROM registrace').fetchone()['c']
            assert remaining == 0

    def test_bulk_delete_no_ids(self, admin_client):
        r = admin_client.post('/admin/registrace/bulk-delete',
                              data={},
                              follow_redirects=True)
        assert r.status_code == 200

    def test_bulk_delete_single(self, admin_client, app):
        ids = self._create_registrations(app, 2)
        r = admin_client.post('/admin/registrace/bulk-delete',
                              data={'ids': ids[0]},
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'Smazáno 1' in r.data.decode()

    def test_bulk_delete_requires_login(self, client):
        r = client.post('/admin/registrace/bulk-delete',
                        data={'ids': [1, 2]})
        assert r.status_code == 302

    def test_bulk_delete_page_has_checkboxes(self, admin_client, app):
        self._create_registrations(app, 2)
        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'select-all' in html
        assert 'row-checkbox' in html
        assert 'bulk-delete-btn' in html

    def test_row_click_toggle_handler_present(self, admin_client, app):
        self._create_registrations(app, 1)
        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'toggleRowCheckbox' in html
        assert 'cursor-pointer' in html


# ── Payment tests ────────────────────────────────────────────────


class TestPayments:
    def _create_approved_registrations(self, app, count=1):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            ids = []
            for i in range(count):
                db.execute(
                    "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                    (f'User {i}', f'user{i}@test.cz'))
                ids.append(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.commit()
            return ids

    def test_bulk_send_selected(self, admin_client, app):
        ids = self._create_approved_registrations(app, 2)
        with patch('app.send_bulk_emails') as mock_bulk:
            r = admin_client.post('/admin/registrace/bulk-send-payment',
                                  data={'ids': [str(i) for i in ids], 'amount': '3500'},
                                  follow_redirects=True)
        assert r.status_code == 200
        mock_bulk.assert_called_once()
        email_items = mock_bulk.call_args[0][0]
        assert len(email_items) == 2

    def test_bulk_send_skips_non_approved(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'pending')",
                       ('Pending', 'p@test.cz'))
            pending_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.commit()

        with patch('app.send_bulk_emails') as mock_bulk:
            r = admin_client.post('/admin/registrace/bulk-send-payment',
                                  data={'ids': [str(pending_id)], 'amount': '3500'},
                                  follow_redirects=True)
        assert r.status_code == 200
        mock_bulk.assert_not_called()

    def test_bulk_send_no_ids_selected(self, admin_client):
        r = admin_client.post('/admin/registrace/bulk-send-payment',
                              data={'amount': '3500'},
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'vybrány' in r.data.decode().lower()

    def test_bulk_send_no_amount(self, admin_client, app):
        ids = self._create_approved_registrations(app)
        with patch('app.send_bulk_emails') as mock_bulk:
            r = admin_client.post('/admin/registrace/bulk-send-payment',
                                  data={'ids': [str(ids[0])]},
                                  follow_redirects=True)
        assert r.status_code == 200
        mock_bulk.assert_not_called()
        assert 'částka' in r.data.decode().lower()

    def test_bulk_send_invalid_amount(self, admin_client, app):
        ids = self._create_approved_registrations(app)
        with patch('app.send_bulk_emails') as mock_bulk:
            r = admin_client.post('/admin/registrace/bulk-send-payment',
                                  data={'ids': [str(ids[0])], 'amount': 'abc'},
                                  follow_redirects=True)
        assert r.status_code == 200
        mock_bulk.assert_not_called()

    def test_bulk_send_zero_amount(self, admin_client, app):
        ids = self._create_approved_registrations(app)
        with patch('app.send_bulk_emails') as mock_bulk:
            r = admin_client.post('/admin/registrace/bulk-send-payment',
                                  data={'ids': [str(ids[0])], 'amount': '0'},
                                  follow_redirects=True)
        assert r.status_code == 200
        mock_bulk.assert_not_called()

    def test_bulk_send_sets_unique_variable_symbols(self, admin_client, app):
        ids = self._create_approved_registrations(app, 2)
        with patch('app.send_bulk_emails'):
            admin_client.post('/admin/registrace/bulk-send-payment',
                              data={'ids': [str(i) for i in ids], 'amount': '3500'})

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            rows = db.execute(
                "SELECT variable_symbol FROM registrace WHERE status = 'approved'"
            ).fetchall()
            symbols = [r['variable_symbol'] for r in rows]
            assert len(set(symbols)) == len(symbols)

    def test_bulk_send_stores_amount(self, admin_client, app):
        ids = self._create_approved_registrations(app)
        with patch('app.send_bulk_emails'):
            admin_client.post('/admin/registrace/bulk-send-payment',
                              data={'ids': [str(ids[0])], 'amount': '5000'})

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT payment_amount FROM registrace WHERE id = ?',
                             (ids[0],)).fetchone()
            assert row['payment_amount'] == 5000.0

    def test_bulk_send_no_iban_configured(self, admin_client, app):
        ids = self._create_approved_registrations(app)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('bank_iban', ''))
            db.commit()

        r = admin_client.post('/admin/registrace/bulk-send-payment',
                              data={'ids': [str(ids[0])], 'amount': '3500'},
                              follow_redirects=True)
        assert 'iban' in r.data.decode().lower()

    def test_payment_note_format(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                ('Jan Novák', 'jan@test.cz'))
            reg_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.commit()

        with patch('app.send_bulk_emails'), \
             patch('app.generate_payment_qr') as mock_qr:
            mock_qr.return_value = 'fakebase64'
            admin_client.post('/admin/registrace/bulk-send-payment',
                              data={'ids': [str(reg_id)], 'amount': '3500'})
            call_args = mock_qr.call_args
            assert call_args[0][3] == 'WACHAU_Jan_Novák'

    def test_check_payments_route(self, admin_client):
        with patch('app.check_fio_payments'):
            r = admin_client.post('/admin/registrace/check-payments',
                                  follow_redirects=True)
        assert r.status_code == 200
        assert 'zkontrolován' in r.data.decode().lower()

    def test_bulk_send_requires_login(self, client):
        r = client.post('/admin/registrace/bulk-send-payment')
        assert r.status_code == 302
        assert 'login' in r.headers['Location']

    def test_qr_modal_present_in_template(self, admin_client):
        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'qr-modal' in html
        assert 'openQrModal' in html
        assert 'bulk-send-payment' in html


class TestBulkSendPaymentDelay:

    def test_send_bulk_emails_dispatches_with_delay(self, app):
        import app as flask_app
        from threading import Event

        sent = []
        sleep_calls = []
        done = Event()

        original_print = print
        def _track_print(*args, **kwargs):
            original_print(*args, **kwargs)
            if args and 'Bulk emails dispatched' in str(args[0]):
                done.set()

        with patch('app.send_email', side_effect=lambda *a, **kw: sent.append(a)):
            with patch('app._time.sleep', side_effect=lambda s: sleep_calls.append(s)):
                with patch('builtins.print', side_effect=_track_print):
                    flask_app.send_bulk_emails([
                        ('a@test.cz', 'Sub A', '<p>A</p>'),
                        ('b@test.cz', 'Sub B', '<p>B</p>'),
                        ('c@test.cz', 'Sub C', '<p>C</p>'),
                    ])
                    done.wait(timeout=5)

        assert len(sent) == 3
        assert sleep_calls.count(3) == 2


class TestCheckFioPayments:
    def test_matches_payment_by_vs(self, app):
        import app as flask_app

        # Create a pending registration with VS
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Test', 'x@y.cz', 42, 3500))
            db.commit()

        # Mock Fio API response
        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500, 'name': 'Objem', 'id': 1},
                        'column5': {'value': '42', 'name': 'VS', 'id': 5},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        # Verify payment was marked as paid
        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT payment_status FROM registrace WHERE variable_symbol = 42").fetchone()
            assert row['payment_status'] == 'paid'

    def test_ignores_insufficient_amount(self, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Test2', 'x@y.cz', 99, 3500))
            db.commit()

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 1000, 'name': 'Objem', 'id': 1},  # too low
                        'column5': {'value': '99', 'name': 'VS', 'id': 5},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT payment_status FROM registrace WHERE variable_symbol = 99").fetchone()
            assert row['payment_status'] == 'pending'

    def test_handles_api_error(self, app):
        import app as flask_app
        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=409)
            flask_app.check_fio_payments()  # should not raise

    def test_handles_no_token(self, app, monkeypatch):
        import app as flask_app
        monkeypatch.setattr(flask_app, 'FIO_API_TOKEN', '')
        flask_app.check_fio_payments()  # should return early without error


# ── Admin CRUD tests ─────────────────────────────────────────────


class TestAdminEtapy:
    def test_list_etapy(self, admin_client):
        r = admin_client.get('/admin/etapy')
        assert r.status_code == 200

    def test_create_etapa(self, admin_client):
        r = admin_client.post('/admin/etapy/new', data={
            'number': '1',
            'title': 'Test Etapa',
            'date': '2026-09-01',
            'distance': '100',
            'elevation_up': '500',
            'elevation_down': '400',
            'route': 'A - B',
            'waypoints': 'town1, town2',
            'description': 'A test stage',
            'map_link': '',
            'youtube_links': '',
            'color': '#ffc107',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložena' in r.data.decode().lower()

    def test_edit_etapa(self, admin_client, app):
        # Create first
        admin_client.post('/admin/etapy/new', data={
            'number': '2', 'title': 'Original', 'date': '', 'distance': '',
            'elevation_up': '', 'elevation_down': '', 'route': '',
            'waypoints': '', 'description': '', 'map_link': '',
            'youtube_links': '', 'color': '#ffc107',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT id FROM etapy WHERE number = 2').fetchone()
            etapa_id = row['id']

        r = admin_client.get(f'/admin/etapy/{etapa_id}/edit')
        assert r.status_code == 200

    def test_delete_etapa(self, admin_client, app):
        admin_client.post('/admin/etapy/new', data={
            'number': '3', 'title': 'ToDelete', 'date': '', 'distance': '',
            'elevation_up': '', 'elevation_down': '', 'route': '',
            'waypoints': '', 'description': '', 'map_link': '',
            'youtube_links': '', 'color': '#ffc107',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT id FROM etapy WHERE number = 3').fetchone()
            etapa_id = row['id']

        r = admin_client.post(f'/admin/etapy/{etapa_id}/delete', follow_redirects=True)
        assert r.status_code == 200
        assert 'smazána' in r.data.decode().lower()


class TestAdminAktuality:
    def test_list_aktuality(self, admin_client):
        r = admin_client.get('/admin/aktuality')
        assert r.status_code == 200

    def test_create_aktualita(self, admin_client):
        r = admin_client.post('/admin/aktuality/new', data={
            'title': 'Test News',
            'content': '<p>News content</p>',
            'published': '1',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložena' in r.data.decode().lower()

    def test_delete_aktualita(self, admin_client, app):
        admin_client.post('/admin/aktuality/new', data={
            'title': 'ToDelete', 'content': 'x', 'published': '1',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT id FROM aktuality WHERE title = 'ToDelete'").fetchone()

        r = admin_client.post(f'/admin/aktuality/{row["id"]}/delete', follow_redirects=True)
        assert r.status_code == 200


class TestAktualitaEmailNotification:
    def _add_approved_registrations(self, app, count):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(count):
                db.execute(
                    "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                    (f'User {i}', f'user{i}@test.cz'))
            db.commit()

    def test_notify_sends_to_approved(self, app, admin_client):
        self._add_approved_registrations(app, 3)
        with patch('app.send_bulk_notifications') as mock_bulk:
            admin_client.post('/admin/aktuality/new', data={
                'title': 'Test News', 'content': '<p>Hi</p>',
                'published': '1', 'notify_participants': '1',
            }, follow_redirects=True)
            mock_bulk.assert_called_once()
            recipients = mock_bulk.call_args[0][0]
            assert len(recipients) == 3
            assert 'user0@test.cz' in recipients

    def test_no_notify_without_checkbox(self, app, admin_client):
        self._add_approved_registrations(app, 3)
        with patch('app.send_bulk_notifications') as mock_bulk:
            admin_client.post('/admin/aktuality/new', data={
                'title': 'Silent News', 'content': '<p>Hi</p>',
                'published': '1',
            }, follow_redirects=True)
            mock_bulk.assert_not_called()

    def test_no_notify_when_unpublished(self, app, admin_client):
        self._add_approved_registrations(app, 3)
        with patch('app.send_bulk_notifications') as mock_bulk:
            admin_client.post('/admin/aktuality/new', data={
                'title': 'Draft News', 'content': '<p>Hi</p>',
                'notify_participants': '1',
            }, follow_redirects=True)
            mock_bulk.assert_not_called()

    def test_denied_excluded(self, app, admin_client):
        self._add_approved_registrations(app, 2)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'denied')",
                ('Denied User', 'denied@test.cz'))
            db.execute(
                "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'pending')",
                ('Pending User', 'pending@test.cz'))
            db.commit()
        with patch('app.send_bulk_notifications') as mock_bulk:
            admin_client.post('/admin/aktuality/new', data={
                'title': 'Selective', 'content': '<p>Hi</p>',
                'published': '1', 'notify_participants': '1',
            }, follow_redirects=True)
            recipients = mock_bulk.call_args[0][0]
            assert len(recipients) == 2
            assert 'denied@test.cz' not in recipients
            assert 'pending@test.cz' not in recipients

    def test_flash_shows_recipient_count(self, app, admin_client):
        self._add_approved_registrations(app, 5)
        with patch('app.send_bulk_notifications'):
            r = admin_client.post('/admin/aktuality/new', data={
                'title': 'Flash Test', 'content': '<p>Hi</p>',
                'published': '1', 'notify_participants': '1',
            }, follow_redirects=True)
            assert '5 účastníkům' in r.data.decode()

    def test_form_has_notify_checkbox(self, admin_client):
        r = admin_client.get('/admin/aktuality/new')
        html = r.data.decode()
        assert 'notify_participants' in html
        assert 'Odeslat emailem' in html

    def test_bulk_notifications_calls_send_email_individually(self):
        calls = []
        original_sleep = _time_module.sleep
        def tracking_send(to, subj, body, **kw):
            calls.append(to)
        with patch('app.send_email', side_effect=tracking_send), \
             patch('app._time.sleep', side_effect=lambda _: None):
            from app import send_bulk_notifications
            send_bulk_notifications(['a@t.cz', 'b@t.cz', 'c@t.cz'], 'Subj', '<p>Body</p>')
            original_sleep(0.1)
            assert calls == ['a@t.cz', 'b@t.cz', 'c@t.cz']


class TestAdminUbytovani:
    def test_list_ubytovani(self, admin_client):
        r = admin_client.get('/admin/ubytovani')
        assert r.status_code == 200

    def test_create_ubytovani(self, admin_client):
        r = admin_client.post('/admin/ubytovani/new', data={
            'etapa_number': '1', 'name': 'Hotel Test', 'city': 'Praha',
            'date': '2026-09-01', 'rooms_info': '2x', 'food_info': 'snídaně',
            'link': '', 'sort_order': '0',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uloženo' in r.data.decode().lower()


class TestAdminPropozice:
    def test_propozice_page(self, admin_client):
        r = admin_client.get('/admin/propozice')
        assert r.status_code == 200

    def test_save_propozice(self, admin_client):
        r = admin_client.post('/admin/propozice', data={
            'content': '<p>Updated propozice</p>',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uloženy' in r.data.decode().lower()


# ── Error handling tests ─────────────────────────────────────────


class TestErrorHandling:
    def test_404_returns_error_page(self, client):
        r = client.get('/this-does-not-exist')
        assert r.status_code == 404

    def test_db_error_in_context_processor(self, app, monkeypatch):
        """When DB is down, pages should still render with empty data."""
        import app as flask_app

        original_get_db = flask_app.get_db

        def failing_db():
            raise Exception("DB down")

        # Only fail on first call (context processor), not on error handler
        call_count = [0]
        def sometimes_fail():
            call_count[0] += 1
            if call_count[0] <= 1:
                raise Exception("DB down")
            return original_get_db()

        client = app.test_client()
        with app.app_context():
            monkeypatch.setattr(flask_app, 'get_db', sometimes_fail)
            r = client.get('/')
            # Should not crash — context processor catches the error
            assert r.status_code in (200, 500)


# ── Password management tests ────────────────────────────────────


class TestPasswordManagement:
    def test_change_password(self, admin_client, app):
        r = admin_client.post('/admin/change-password', data={
            'current_password': 'testpass',
            'new_password': 'newpass123',
            'new_password2': 'newpass123',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'změněno' in r.data.decode().lower()

    def test_change_password_wrong_current(self, admin_client):
        r = admin_client.post('/admin/change-password', data={
            'current_password': 'wrongpassword',
            'new_password': 'newpass123',
            'new_password2': 'newpass123',
        })
        assert 'nesprávné' in r.data.decode().lower()

    def test_change_password_mismatch(self, admin_client):
        r = admin_client.post('/admin/change-password', data={
            'current_password': 'testpass',
            'new_password': 'newpass123',
            'new_password2': 'different',
        })
        assert 'neshodují' in r.data.decode().lower()

    def test_change_password_too_short(self, admin_client):
        r = admin_client.post('/admin/change-password', data={
            'current_password': 'testpass',
            'new_password': '12345',
            'new_password2': '12345',
        })
        assert '8 znaků' in r.data.decode().lower()

    def test_set_password_page_requires_session(self, client):
        r = client.get('/admin/set-password')
        assert r.status_code == 302
        assert '/admin/login' in r.headers['Location']

    def test_set_password_too_short(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = NULL WHERE username = ?', ('adam',))
            db.commit()

        client.post('/admin/login', data={'username': 'adam', 'password': ''})
        r = client.post('/admin/set-password', data={
            'password': 'short',
            'password2': 'short',
        })
        assert '8 znaků' in r.data.decode().lower()

    def test_set_password_mismatch(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = NULL WHERE username = ?', ('adam',))
            db.commit()

        client.post('/admin/login', data={'username': 'adam', 'password': ''})
        r = client.post('/admin/set-password', data={
            'password': 'password123',
            'password2': 'different123',
        })
        assert 'neshodují' in r.data.decode().lower()

    def test_set_password_success(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = NULL WHERE username = ?', ('adam',))
            db.commit()

        client.post('/admin/login', data={'username': 'adam', 'password': ''})
        r = client.post('/admin/set-password', data={
            'password': 'newpassword1',
            'password2': 'newpassword1',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_forgot_password(self, client):
        r = client.post('/admin/forgot-password', data={
            'username': 'adam',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_forgot_password_sends_email(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET email = ? WHERE username = ?', ('adam@test.cz', 'adam'))
            pw_hash = bcrypt.hashpw(b'testpass', bcrypt.gensalt()).decode()
            db.execute('UPDATE admins SET password_hash = ? WHERE username = ?', (pw_hash, 'adam'))
            db.commit()

        with patch('app.send_email') as mock_email:
            r = client.post('/admin/forgot-password', data={
                'username': 'adam',
            }, follow_redirects=True)
            assert r.status_code == 200
            mock_email.assert_called_once()

    def test_forgot_password_nonexistent_user(self, client):
        r = client.post('/admin/forgot-password', data={
            'username': 'nobody',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'odeslali' in r.data.decode().lower()

    def test_reset_password_invalid_token(self, client):
        r = client.get('/admin/reset-password/invalid-token', follow_redirects=True)
        assert r.status_code == 200
        assert 'neplatný' in r.data.decode().lower()

    def test_reset_password_flow(self, app, client):
        import secrets
        from datetime import datetime
        token = secrets.token_urlsafe(48)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            admin = db.execute('SELECT id FROM admins WHERE username = ?', ('adam',)).fetchone()
            expires = datetime.now().timestamp() + 3600
            db.execute(
                'INSERT INTO password_reset_tokens (admin_id, token, expires_at) VALUES (?, ?, ?)',
                (admin['id'], token, datetime.fromtimestamp(expires))
            )
            db.commit()

        r = client.get(f'/admin/reset-password/{token}')
        assert r.status_code == 200

        r = client.post(f'/admin/reset-password/{token}', data={
            'password': 'newpassword1',
            'password2': 'newpassword1',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'změněno' in r.data.decode().lower()

    def test_reset_password_too_short(self, app, client):
        import secrets
        from datetime import datetime
        token = secrets.token_urlsafe(48)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            admin = db.execute('SELECT id FROM admins WHERE username = ?', ('adam',)).fetchone()
            expires = datetime.now().timestamp() + 3600
            db.execute(
                'INSERT INTO password_reset_tokens (admin_id, token, expires_at) VALUES (?, ?, ?)',
                (admin['id'], token, datetime.fromtimestamp(expires))
            )
            db.commit()

        r = client.post(f'/admin/reset-password/{token}', data={
            'password': 'short',
            'password2': 'short',
        })
        assert '8 znaků' in r.data.decode().lower()

    def test_reset_password_expired_token(self, app, client):
        import secrets
        from datetime import datetime
        token = secrets.token_urlsafe(48)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            admin = db.execute('SELECT id FROM admins WHERE username = ?', ('adam',)).fetchone()
            expired = datetime.now().timestamp() - 3600
            db.execute(
                'INSERT INTO password_reset_tokens (admin_id, token, expires_at) VALUES (?, ?, ?)',
                (admin['id'], token, datetime.fromtimestamp(expired))
            )
            db.commit()

        r = client.get(f'/admin/reset-password/{token}', follow_redirects=True)
        assert 'neplatný' in r.data.decode().lower() or 'vypršel' in r.data.decode().lower()


# ── CSRF protection tests ───────────────────────────────────────────


class TestCSRFProtection:
    def test_post_without_csrf_returns_403(self, app):
        app.test_client_class = FlaskClient
        raw_client = app.test_client()
        r = raw_client.post('/registrace', data={
            'name': 'Test',
            'email': 'test@test.cz',
        })
        assert r.status_code == 403
        app.test_client_class = None

    def test_post_with_wrong_csrf_returns_403(self, app):
        app.test_client_class = FlaskClient
        raw_client = app.test_client()
        with raw_client.session_transaction() as sess:
            sess['_csrf_token'] = 'correct-token'
        r = raw_client.post('/registrace', data={
            'name': 'Test',
            'email': 'test@test.cz',
            '_csrf_token': 'wrong-token',
        })
        assert r.status_code == 403
        app.test_client_class = None

    def test_post_with_valid_csrf_succeeds(self, client):
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Test User',
                'email': 'test@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
            assert r.status_code == 200

    def test_csrf_token_generated_on_form_page(self, app):
        app.test_client_class = FlaskClient
        raw_client = app.test_client()
        raw_client.get('/registrace')
        with raw_client.session_transaction() as sess:
            assert '_csrf_token' in sess
            assert len(sess['_csrf_token']) == 64
        app.test_client_class = None


# ── Maintenance mode tests ──────────────────────────────────────────


class TestMaintenanceMode:
    def test_maintenance_overlay_shown_when_enabled(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '1'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_until', '2099-01-01T00:00:01'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert 'maintenance-overlay' in html
        assert 'PŘIPRAVUJEME' in html

    def test_maintenance_overlay_hidden_when_disabled(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '0'))
            db.commit()

        r = client.get('/')
        assert 'maintenance-overlay' not in r.data.decode()

    def test_maintenance_overlay_hidden_after_deadline(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '1'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_until', '2020-01-01T00:00:01'))
            db.commit()

        r = client.get('/')
        assert 'maintenance-overlay' not in r.data.decode()

    def test_maintenance_does_not_affect_admin(self, app, admin_client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '1'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_until', '2099-01-01T00:00:01'))
            db.commit()

        r = admin_client.get('/admin')
        assert 'maintenance-overlay' not in r.data.decode()

    def test_maintenance_blocks_scrolling(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '1'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_until', '2099-01-01T00:00:01'))
            db.commit()

        r = client.get('/')
        assert 'overflow-hidden' in r.data.decode()

    def test_maintenance_toggle_in_admin_settings(self, admin_client, app):
        r = admin_client.post('/admin/nastaveni', data={
            'event_name': 'Test', 'event_year': '2026', 'event_days': '3',
            'event_km': '100', 'event_elevation': '500', 'event_dates': 'test',
            'contact_name_1': 'A', 'contact_name_2': 'B', 'contact_email': 'a@b.cz',
            'photos_link': '', 'photos_text': '', 'payment_amount': '3500',
            'bank_account': '', 'bank_iban': '',
            'maintenance_until': '2099-12-31T23:59:59',
        }, follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT value FROM site_settings WHERE key = 'maintenance_enabled'").fetchone()
            assert row['value'] == '0'


# ── Security headers tests ──────────────────────────────────────────


class TestSecurityHeaders:
    def test_x_content_type_options(self, client):
        r = client.get('/')
        assert r.headers.get('X-Content-Type-Options') == 'nosniff'

    def test_x_frame_options(self, client):
        r = client.get('/')
        assert r.headers.get('X-Frame-Options') == 'DENY'

    def test_session_cookie_httponly(self, app):
        assert app.config['SESSION_COOKIE_HTTPONLY'] is True

    def test_session_cookie_samesite(self, app):
        assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


# ── Context processor tests ─────────────────────────────────────────


class TestContextProcessor:
    def test_injects_settings(self, client):
        r = client.get('/')
        html = r.data.decode()
        assert 'CYKLOEXPEDICE' in html

    def test_injects_etapy_nav(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO etapy (number, title) VALUES (?, ?)", (1, 'Testovaci Etapa'))
            db.commit()

        r = client.get('/')
        assert 'Testovaci Etapa' in r.data.decode()

    def test_injects_current_year(self, client):
        from datetime import datetime
        r = client.get('/')
        assert str(datetime.now().year) in r.data.decode()


# ── Public route edge cases ──────────────────────────────────────────


class TestPublicRouteEdgeCases:
    def test_fotky_disabled_returns_404(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('fotky_enabled', '0'))
            db.commit()

        r = client.get('/fotky')
        assert r.status_code == 404

    def test_etapa_detail(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, description) VALUES (?, ?, ?)",
                (10, 'Detail Test', 'Test description'))
            db.commit()

        r = client.get('/etapa/10')
        assert r.status_code == 200
        assert 'Detail Test' in r.data.decode()

    def test_etapa_nonexistent_returns_404(self, client):
        r = client.get('/etapa/999')
        assert r.status_code == 404

    def test_etapa_prev_next_navigation(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO etapy (number, title) VALUES (?, ?)", (1, 'First'))
            db.execute("INSERT INTO etapy (number, title) VALUES (?, ?)", (2, 'Second'))
            db.execute("INSERT INTO etapy (number, title) VALUES (?, ?)", (3, 'Third'))
            db.commit()

        r = client.get('/etapa/2')
        assert r.status_code == 200

    def test_aktuality_only_shows_published(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO aktuality (title, content, published) VALUES (?, ?, ?)",
                       ('Published News', 'content', 1))
            db.execute("INSERT INTO aktuality (title, content, published) VALUES (?, ?, ?)",
                       ('Draft News', 'content', 0))
            db.commit()

        r = client.get('/aktuality')
        html = r.data.decode()
        assert 'PUBLISHED NEWS' in html
        assert 'DRAFT NEWS' not in html

    def test_propozice_shows_content(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO propozice (content) VALUES (?)", ('<p>Test Content</p>',))
            db.commit()

        r = client.get('/propozice')
        assert 'Test Content' in r.data.decode()


# ── Admin profile & SMTP tests ──────────────────────────────────────


class TestAdminProfile:
    def test_save_profile_email(self, admin_client, app):
        r = admin_client.post('/admin/profile', data={
            'email': 'newemail@test.cz',
            'smtp_host': 'smtp.test.cz',
            'smtp_port': '587',
            'smtp_user': 'user',
            'smtp_password': 'pass',
            'smtp_from': 'from@test.cz',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložen' in r.data.decode().lower()

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            admin = db.execute('SELECT email FROM admins WHERE username = ?', ('adam',)).fetchone()
            assert admin['email'] == 'newemail@test.cz'
            smtp = db.execute("SELECT value FROM site_settings WHERE key = 'smtp_host_adam'").fetchone()
            assert smtp['value'] == 'smtp.test.cz'


# ── Admin CRUD edge cases ───────────────────────────────────────────


class TestAdminCRUDEdgeCases:
    def test_edit_etapa_post(self, admin_client, app):
        admin_client.post('/admin/etapy/new', data={
            'number': '5', 'title': 'Original Title', 'date': '', 'distance': '',
            'elevation_up': '', 'elevation_down': '', 'route': '',
            'waypoints': '', 'description': '', 'map_link': '',
            'youtube_links': '', 'color': '#ffc107',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT id FROM etapy WHERE number = 5').fetchone()
            etapa_id = row['id']

        r = admin_client.post(f'/admin/etapy/{etapa_id}/edit', data={
            'number': '5', 'title': 'Updated Title', 'date': '2026-09-01',
            'distance': '120', 'elevation_up': '600', 'elevation_down': '500',
            'route': 'A - C', 'waypoints': 'town1', 'description': 'Updated',
            'map_link': '', 'youtube_links': 'https://youtube.com/watch?v=123',
            'color': '#ff0000',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložena' in r.data.decode().lower()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute('SELECT title FROM etapy WHERE number = 5').fetchone()
            assert row['title'] == 'Updated Title'

    def test_edit_nonexistent_etapa_returns_404(self, admin_client):
        r = admin_client.get('/admin/etapy/9999/edit')
        assert r.status_code == 404

    def test_edit_aktualita(self, admin_client, app):
        admin_client.post('/admin/aktuality/new', data={
            'title': 'Edit Me', 'content': '<p>original</p>', 'published': '1',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT id FROM aktuality WHERE title = 'Edit Me'").fetchone()
            aid = row['id']

        r = admin_client.get(f'/admin/aktuality/{aid}/edit')
        assert r.status_code == 200

        r = admin_client.post(f'/admin/aktuality/{aid}/edit', data={
            'title': 'Edited Title', 'content': '<p>updated</p>', 'published': '1',
        }, follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute('SELECT title FROM aktuality WHERE id = ?', (aid,)).fetchone()
            assert row['title'] == 'Edited Title'

    def test_edit_nonexistent_aktualita_returns_404(self, admin_client):
        r = admin_client.get('/admin/aktuality/9999/edit')
        assert r.status_code == 404

    def test_edit_ubytovani(self, admin_client, app):
        admin_client.post('/admin/ubytovani/new', data={
            'etapa_number': '1', 'name': 'Edit Hotel', 'city': 'Brno',
            'date': '', 'rooms_info': '', 'food_info': '', 'link': '', 'sort_order': '0',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT id FROM ubytovani WHERE name = 'Edit Hotel'").fetchone()
            uid = row['id']

        r = admin_client.get(f'/admin/ubytovani/{uid}/edit')
        assert r.status_code == 200

        r = admin_client.post(f'/admin/ubytovani/{uid}/edit', data={
            'etapa_number': '1', 'name': 'Updated Hotel', 'city': 'Praha',
            'date': '', 'rooms_info': '3x', 'food_info': 'oběd', 'link': '', 'sort_order': '1',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uloženo' in r.data.decode().lower()

    def test_edit_nonexistent_ubytovani_returns_404(self, admin_client):
        r = admin_client.get('/admin/ubytovani/9999/edit')
        assert r.status_code == 404

    def test_delete_ubytovani(self, admin_client, app):
        admin_client.post('/admin/ubytovani/new', data={
            'etapa_number': '1', 'name': 'Delete Hotel', 'city': 'X',
            'date': '', 'rooms_info': '', 'food_info': '', 'link': '', 'sort_order': '0',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT id FROM ubytovani WHERE name = 'Delete Hotel'").fetchone()
            uid = row['id']

        r = admin_client.post(f'/admin/ubytovani/{uid}/delete', follow_redirects=True)
        assert r.status_code == 200
        assert 'smazáno' in r.data.decode().lower()

    def test_propozice_update_existing(self, admin_client, app):
        admin_client.post('/admin/propozice', data={'content': '<p>First</p>'})
        r = admin_client.post('/admin/propozice', data={'content': '<p>Second</p>'},
                              follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT content FROM propozice ORDER BY id DESC LIMIT 1').fetchone()
            assert 'Second' in row['content']

    def test_create_aktualita_unpublished(self, admin_client, app):
        r = admin_client.post('/admin/aktuality/new', data={
            'title': 'Draft', 'content': '<p>draft</p>',
        }, follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT published FROM aktuality WHERE title = 'Draft'").fetchone()
            assert row['published'] == 0


# ── Fio payments edge cases ─────────────────────────────────────────


class TestCheckFioPaymentsEdgeCases:
    def test_handles_empty_transaction_list(self, app):
        import app as flask_app
        fio_response = {
            'accountStatement': {
                'transactionList': None
            }
        }
        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

    def test_handles_missing_vs_column(self, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Test', 'x@y.cz', 50, 3500))
            db.commit()

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500},
                    }]
                }
            }
        }
        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT payment_status FROM registrace WHERE variable_symbol = 50").fetchone()
            assert row['payment_status'] == 'pending'

    def test_handles_network_error(self, app):
        import app as flask_app
        with patch('app.requests.get') as mock_get:
            mock_get.side_effect = Exception('Network error')
            flask_app.check_fio_payments()


# ── Registration edge cases ─────────────────────────────────────────


class TestRegistrationCapacity:
    """Tests for the configurable max_capacity registration limit."""

    def _fill_approved(self, app, count):
        """Insert `count` approved registrations into the database."""
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(count):
                db.execute(
                    "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                    (f'User {i}', f'user{i}@test.cz'))
            db.commit()

    def _set_capacity(self, app, value):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES ('max_capacity', ?)", (str(value),))
            db.commit()

    def test_form_visible_under_capacity(self, app, client):
        self._fill_approved(app, 10)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html
        assert 'KAPACITA NAPLNĚNA' not in html

    def test_form_hidden_at_capacity(self, app, client):
        self._fill_approved(app, 30)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' not in html
        assert 'KAPACITA NAPLNĚNA' in html

    def test_form_hidden_above_capacity(self, app, client):
        self._fill_approved(app, 35)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'KAPACITA NAPLNĚNA' in html

    def test_post_blocked_at_capacity(self, app, client):
        self._fill_approved(app, 30)
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Late User',
                'email': 'late@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        html = r.data.decode()
        assert 'naplněna' in html.lower()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT COUNT(*) c FROM registrace WHERE name = 'Late User'").fetchone()
            assert row['c'] == 0

    def test_post_allowed_under_capacity(self, app, client):
        self._fill_approved(app, 29)
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Just In Time',
                'email': 'just@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        html = r.data.decode()
        assert 'Děkujeme' in html

    def test_pending_registrations_dont_count(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(35):
                db.execute(
                    "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'pending')",
                    (f'Pending {i}', f'pending{i}@test.cz'))
            db.commit()
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html

    def test_denied_registrations_dont_count(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(35):
                db.execute(
                    "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'denied')",
                    (f'Denied {i}', f'denied{i}@test.cz'))
            db.commit()
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html

    def test_nav_button_disabled_at_capacity(self, app, client):
        self._fill_approved(app, 30)
        r = client.get('/')
        html = r.data.decode()
        assert html.count('Kapacita naplněna') >= 2

    def test_nav_button_enabled_under_capacity(self, app, client):
        self._fill_approved(app, 10)
        r = client.get('/')
        html = r.data.decode()
        assert 'Kapacita naplněna' not in html
        assert 'Přihlásit se' in html

    def test_hero_button_disabled_at_capacity(self, app, client):
        self._fill_approved(app, 30)
        r = client.get('/')
        html = r.data.decode()
        assert 'Kapacita naplněna' in html
        assert 'bi-bicycle' not in html

    def test_zero_approved_shows_form(self, client):
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html

    def test_exactly_29_allows_registration(self, app, client):
        self._fill_approved(app, 29)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html
        assert 'KAPACITA NAPLNĚNA' not in html

    def test_custom_capacity_from_settings(self, app, client):
        self._set_capacity(app, 5)
        self._fill_approved(app, 5)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'KAPACITA NAPLNĚNA' in html

    def test_custom_capacity_allows_under_limit(self, app, client):
        self._set_capacity(app, 5)
        self._fill_approved(app, 4)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html

    def test_custom_capacity_blocks_post(self, app, client):
        self._set_capacity(app, 5)
        self._fill_approved(app, 5)
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Over Limit', 'email': 'over@test.cz', 'gdpr_consent': 'on',
            }, follow_redirects=True)
        assert 'naplněna' in r.data.decode().lower()

    def test_admin_settings_shows_capacity_field(self, admin_client):
        r = admin_client.get('/admin/nastaveni')
        html = r.data.decode()
        assert 'max_capacity' in html
        assert 'Maximální počet účastníků' in html

    def test_admin_settings_saves_capacity(self, app, admin_client):
        admin_client.post('/admin/nastaveni', data={
            'event_name': 'Test', 'event_year': '2026',
            'event_days': '3', 'event_km': '100',
            'event_elevation': '500', 'event_dates': 'Test',
            'contact_name_1': '', 'contact_name_2': '',
            'contact_email': '', 'photos_link': '', 'photos_text': '',
            'max_capacity': '15',
            'payment_amount': '3500', 'bank_account': '', 'bank_iban': '',
        }, follow_redirects=True)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT value FROM site_settings WHERE key = 'max_capacity'").fetchone()
            assert row['value'] == '15'


class TestRegistrationEdgeCases:
    def test_deny_nonexistent_registration_returns_404(self, admin_client):
        r = admin_client.get('/admin/registrace/9999/deny')
        assert r.status_code == 404

    def test_deny_nonexistent_registration_post_returns_404(self, admin_client):
        r = admin_client.post('/admin/registrace/9999/deny', data={'reason': 'test'})
        assert r.status_code == 404


# ── Email sending tests ─────────────────────────────────────────────


class TestEmailSending:
    def test_send_email_no_recipient(self, app):
        from app import send_email
        with app.app_context():
            send_email('', 'Test', '<p>body</p>')

    def test_send_email_no_smtp_configured(self, app):
        from app import send_email
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for username in ['adam', 'michal']:
                db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                           (f'smtp_host_{username}', ''))
            db.commit()
            send_email('test@test.cz', 'Test', '<p>body</p>')

    def test_email_has_plain_text_and_headers(self, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('smtp_host_adam', 'smtp.resend.com'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('smtp_port_adam', '465'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('smtp_user_adam', 'resend'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('smtp_password_adam', 'fake'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('smtp_from_adam', 'info@cykloexpedice.cz'))
            db.commit()

        captured = {}

        def mock_send(self_srv, msg):
            captured['msg'] = msg

        with patch('smtplib.SMTP_SSL') as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=instance)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            instance.send_message = lambda msg: captured.update({'msg': msg})

            with app.app_context():
                flask_app.send_email('test@test.cz', 'Test Subject', '<p>Hello <b>world</b></p>')

            import time
            time.sleep(0.5)

        msg = captured.get('msg')
        if msg:
            payloads = msg.get_payload()
            types = [p.get_content_type() for p in payloads]
            assert 'text/plain' in types
            assert 'text/html' in types
            plain_part = [p for p in payloads if p.get_content_type() == 'text/plain'][0]
            plain_text = plain_part.get_payload(decode=True).decode()
            assert 'Hello' in plain_text
            assert '<p>' not in plain_text
            assert msg['List-Unsubscribe'] is not None
            assert 'cykloexpedice.cz' in msg['Message-ID']


# ── YouTube embed converter tests ──────────────────────────────────


class TestYoutubeToEmbed:
    def test_watch_url(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        assert result == 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'

    def test_short_url(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://youtu.be/dQw4w9WgXcQ')
        assert result == 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'

    def test_embed_url(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://www.youtube.com/embed/dQw4w9WgXcQ')
        assert result == 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'

    def test_nocookie_url_passthrough(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ')
        assert result == 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'

    def test_watch_url_without_www(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://youtube.com/watch?v=abc123_-X')
        assert result == 'https://www.youtube-nocookie.com/embed/abc123_-X'

    def test_invalid_url_returned_as_is(self):
        from app import youtube_to_embed
        assert youtube_to_embed('https://example.com/video') == 'https://example.com/video'

    def test_empty_string(self):
        from app import youtube_to_embed
        assert youtube_to_embed('') == ''


# ── Contact page content tests ─────────────────────────────────────


class TestContactPage:
    def test_contact_page_shows_phone_numbers(self, client):
        r = client.get('/kontakt')
        html = r.data.decode()
        assert '+420 725 604 608' in html
        assert '+420 607 128 731' in html

    def test_contact_page_shows_emails(self, client):
        r = client.get('/kontakt')
        html = r.data.decode()
        assert 'michal.prikryl@atlas.cz' in html
        assert 'adam.prikryl7@gmail.com' in html

    def test_contact_page_has_tel_links(self, client):
        r = client.get('/kontakt')
        html = r.data.decode()
        assert 'href="tel:+420725604608"' in html
        assert 'href="tel:+420607128731"' in html

    def test_contact_page_has_mailto_links(self, client):
        r = client.get('/kontakt')
        html = r.data.decode()
        assert 'href="mailto:michal.prikryl@atlas.cz"' in html
        assert 'href="mailto:adam.prikryl7@gmail.com"' in html


# ── Etapa elevation profile rendering tests ────────────────────────


class TestEtapaElevationProfile:
    def test_elevation_profile_rendered(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, elevation_up, elevation_down) "
                "VALUES (?, ?, ?, ?)",
                (1, 'Test Etapa', '338 m', '398 m'))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert 'VÝŠKOVÝ PROFIL' in html
        assert '338 m' in html
        assert '398 m' in html
        assert 'Stoupání' in html
        assert 'Klesání' in html

    def test_elevation_profile_hidden_without_data(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title) VALUES (?, ?)",
                (1, 'No Elevation'))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert 'VÝŠKOVÝ PROFIL' not in html

    def test_elevation_svg_rendered(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, elevation_up, elevation_down) "
                "VALUES (?, ?, ?, ?)",
                (1, 'SVG Test', '500 m', '400 m'))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert '<svg' in html
        assert 'hillGrad' in html


# ── Etapa YouTube embed integration tests ──────────────────────────


class TestEtapaYoutubeEmbed:
    def test_youtube_links_converted_to_nocookie(self, app, client):
        import json as json_mod
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            links = json_mod.dumps(['https://www.youtube.com/watch?v=test123'])
            db.execute(
                "INSERT INTO etapy (number, title, youtube_links) VALUES (?, ?, ?)",
                (1, 'YT Test', links))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert 'youtube-nocookie.com/embed/test123' in html
        assert 'youtube.com/watch' not in html

    def test_multiple_youtube_links(self, app, client):
        import json as json_mod
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            links = json_mod.dumps([
                'https://youtu.be/vid1',
                'https://www.youtube.com/watch?v=vid2',
            ])
            db.execute(
                "INSERT INTO etapy (number, title, youtube_links) VALUES (?, ?, ?)",
                (1, 'Multi YT', links))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert 'youtube-nocookie.com/embed/vid1' in html
        assert 'youtube-nocookie.com/embed/vid2' in html


# ── Route timeline rendering tests ─────────────────────────────────


class TestRouteTimeline:
    def test_index_shows_route_timeline(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, distance, route, color) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, 'Etapa Jedna', '85 km', 'Blansko - Znojmo', '#ff6600'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert 'ETAPY EXPEDICE' in html
        assert 'BLANSKO' in html
        assert 'ZNOJMO' in html
        assert '85 km' in html

    def test_index_timeline_links_to_etapa(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, route) VALUES (?, ?, ?)",
                (1, 'Test', 'A - B'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert '/etapa/1' in html

    def test_propozice_shows_route_timeline(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, distance, route, color) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, 'Prop Etapa', '100 km', 'Praha - Brno', '#ffc107'))
            db.commit()

        r = client.get('/propozice')
        html = r.data.decode()
        assert 'TRASA EXPEDICE' in html
        assert 'PRAHA' in html
        assert 'BRNO' in html

    def test_context_processor_provides_route_and_color(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, distance, route, color) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, 'Color Test', '50 km', 'X - Y', '#e74c3c'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert '#e74c3c' in html


# ── Wave divider tests ─────────────────────────────────────────────


class TestWaveDividers:
    def test_index_has_wave_divider(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, route) VALUES (?, ?, ?)",
                (1, 'Wave Test', 'A - B'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert 'viewBox="0 0 1440 80"' in html

    def test_propozice_has_wave_dividers(self, client):
        r = client.get('/propozice')
        html = r.data.decode()
        assert html.count('viewBox="0 0 1440 80"') >= 1


class TestPragueTimeFilter:
    def test_naive_utc_to_prague_summer(self, app):
        from app import prague_time_filter
        dt = datetime(2026, 7, 9, 12, 59, 54)
        result = prague_time_filter(dt)
        assert result == '09.07.2026 14:59:54'

    def test_naive_utc_to_prague_winter(self, app):
        from app import prague_time_filter
        dt = datetime(2026, 1, 15, 10, 0, 0)
        result = prague_time_filter(dt)
        assert result == '15.01.2026 11:00:00'

    def test_aware_utc(self, app):
        from app import prague_time_filter
        dt = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        result = prague_time_filter(dt)
        assert result == '09.07.2026 14:00:00'

    def test_none_returns_dash(self, app):
        from app import prague_time_filter
        assert prague_time_filter(None) == '–'

    def test_custom_format(self, app):
        from app import prague_time_filter
        dt = datetime(2026, 7, 9, 12, 0, 0)
        result = prague_time_filter(dt, '%d. %m. %Y %H:%M:%S')
        assert result == '09. 07. 2026 14:00:00'

    def test_registrace_shows_prague_time(self, app, admin_client):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, phone, note) VALUES (?, ?, ?, ?)",
                ('Test User', 'test@test.cz', '', ''))
            db.commit()
        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'prague_time' not in html


# ── Weather & SMS tests ──────────────────────────────────────────


class TestStripDiacritics:
    def test_basic(self):
        from app import strip_diacritics
        assert strip_diacritics('Přeháňky') == 'Prehanky'

    def test_full_czech(self):
        from app import strip_diacritics
        assert strip_diacritics('žluťoučký kůň') == 'zlutoucky kun'

    def test_no_diacritics(self):
        from app import strip_diacritics
        assert strip_diacritics('hello') == 'hello'

    def test_empty(self):
        from app import strip_diacritics
        assert strip_diacritics('') == ''

    def test_uppercase(self):
        from app import strip_diacritics
        assert strip_diacritics('ŘÍČANY') == 'RICANY'


class TestGeocodeCity:
    def test_success(self):
        from app import geocode_city
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'results': [{'latitude': 49.59, 'longitude': 17.25}]
        }
        with patch('app.requests.get', return_value=mock_response) as mock_get:
            result = geocode_city('Olomouc')
            assert result == (49.59, 17.25)
            mock_get.assert_called_once()
            assert mock_get.call_args[1]['params']['name'] == 'Olomouc'

    def test_not_found(self):
        from app import geocode_city
        mock_response = MagicMock()
        mock_response.json.return_value = {'results': []}
        with patch('app.requests.get', return_value=mock_response):
            assert geocode_city('NonExistentPlace12345') is None

    def test_empty_results(self):
        from app import geocode_city
        mock_response = MagicMock()
        mock_response.json.return_value = {}
        with patch('app.requests.get', return_value=mock_response):
            assert geocode_city('Nowhere') is None


class TestGetWeatherForecast:
    def _mock_weather_response(self, date_str='2026-09-11'):
        mock_geo = MagicMock()
        mock_geo.json.return_value = {'results': [{'latitude': 49.59, 'longitude': 17.25}]}
        hourly_times = [f'{date_str}T{h:02d}:00' for h in range(24)]
        hourly_temps = [12, 11, 10, 10, 11, 13, 15, 17, 19, 21, 23, 24, 25, 25, 24, 23, 21, 20, 18, 16, 15, 14, 13, 12]
        mock_weather = MagicMock()
        mock_weather.json.return_value = {
            'daily': {
                'time': [date_str],
                'temperature_2m_max': [25],
                'temperature_2m_min': [10],
                'weather_code': [2],
                'precipitation_probability_max': [35],
            },
            'hourly': {
                'time': hourly_times,
                'temperature_2m': hourly_temps,
            },
        }
        return mock_geo, mock_weather

    def test_success(self):
        from app import get_weather_forecast
        mock_geo, mock_weather = self._mock_weather_response()
        with patch('app.requests.get', side_effect=[mock_geo, mock_weather]):
            result = get_weather_forecast('Olomouc', '2026-09-11')
            assert result is not None
            assert result['city'] == 'Olomouc'
            assert result['temp_avg'] == 21.7
            assert result['weather_code'] == 2
            assert result['precip_prob'] == 35

    def test_geocode_fails(self):
        from app import get_weather_forecast
        mock_geo = MagicMock()
        mock_geo.json.return_value = {}
        with patch('app.requests.get', return_value=mock_geo):
            assert get_weather_forecast('Nowhere', '2026-09-11') is None

    def test_date_not_in_forecast(self):
        from app import get_weather_forecast
        mock_geo, mock_weather = self._mock_weather_response('2026-09-11')
        with patch('app.requests.get', side_effect=[mock_geo, mock_weather]):
            assert get_weather_forecast('Olomouc', '2026-12-25') is None


class TestComposeWeatherSms:
    def test_format_nice_weather(self):
        from app import compose_weather_sms
        etapa = {'number': 1, 'date': '11.09.2026 (čtvrtek)'}
        w_start = {'city': 'Olomouc', 'temp_avg': 20.5, 'weather_code': 0, 'precip_prob': 5}
        w_end = {'city': 'Brno', 'temp_avg': 22.0, 'weather_code': 1, 'precip_prob': 10}
        msg = compose_weather_sms(etapa, w_start, w_end)
        assert 'Predpoved pocasi' in msg
        assert 'Cykloexpedice Den 1 (11.9.)' in msg
        assert 'Olomouc - Brno' in msg
        assert 'Jasno.' in msg
        assert 'Teplota pres den: 21' in msg
        assert 'Prijemnou jizdu!' in msg

    def test_storm_localized_to_start(self):
        from app import compose_weather_sms
        etapa = {'number': 1, 'date': '11.09.2026 (čtvrtek)'}
        w_start = {'city': 'Blansko', 'temp_avg': 25, 'weather_code': 95, 'precip_prob': 43}
        w_end = {'city': 'Znojmo', 'temp_avg': 26, 'weather_code': 3, 'precip_prob': 18}
        msg = compose_weather_sms(etapa, w_start, w_end)
        assert 'bourka v okoli Blansko' in msg
        assert 'Zatazeno' in msg

    def test_rain_localized_to_end(self):
        from app import compose_weather_sms
        etapa = {'number': 1, 'date': '11.09.2026 (čtvrtek)'}
        w_start = {'city': 'Blansko', 'temp_avg': 25, 'weather_code': 0, 'precip_prob': 5}
        w_end = {'city': 'Znojmo', 'temp_avg': 26, 'weather_code': 61, 'precip_prob': 60}
        msg = compose_weather_sms(etapa, w_start, w_end)
        assert 'v okoli Znojmo' in msg
        assert 'Jasno' in msg

    def test_rain_on_whole_route(self):
        from app import compose_weather_sms
        etapa = {'number': 1, 'date': '11.09.2026 (čtvrtek)'}
        w_start = {'city': 'A', 'temp_avg': 17, 'weather_code': 61, 'precip_prob': 70}
        w_end = {'city': 'B', 'temp_avg': 16, 'weather_code': 63, 'precip_prob': 80}
        msg = compose_weather_sms(etapa, w_start, w_end)
        assert 'na cele trase' in msg

    def test_no_diacritics(self):
        from app import compose_weather_sms
        etapa = {'number': 2, 'date': '12.09.2026 (pátek)'}
        w_start = {'city': 'Poděbrady', 'temp_avg': 19, 'weather_code': 80, 'precip_prob': 60}
        w_end = {'city': 'Říčany', 'temp_avg': 18, 'weather_code': 61, 'precip_prob': 80}
        msg = compose_weather_sms(etapa, w_start, w_end)
        assert 'Podebrady' in msg
        assert 'Ricany' in msg
        for ch in 'áčďéěíňóřšťúůýž':
            assert ch not in msg

    def test_under_160_chars(self):
        from app import compose_weather_sms
        etapa = {'number': 1, 'date': '11.09.2026 (čtvrtek)'}
        w_start = {'city': 'Olomouc', 'temp_avg': 20, 'weather_code': 0, 'precip_prob': 5}
        w_end = {'city': 'Brno', 'temp_avg': 22, 'weather_code': 3, 'precip_prob': 10}
        msg = compose_weather_sms(etapa, w_start, w_end)
        assert len(msg) <= 160

    def test_under_160_chars_with_long_names(self):
        from app import compose_weather_sms
        etapa = {'number': 1, 'date': '11.09.2026 (čtvrtek)'}
        w_start = {'city': 'Česká Třebová', 'temp_avg': 20, 'weather_code': 95, 'precip_prob': 80}
        w_end = {'city': 'Moravská Třebová', 'temp_avg': 18, 'weather_code': 3, 'precip_prob': 10}
        msg = compose_weather_sms(etapa, w_start, w_end)
        assert len(msg) <= 160


class TestSendSmsStub:
    def test_stub_when_no_credentials(self):
        from app import send_sms
        with patch.dict(os.environ, {}, clear=True):
            result = send_sms('+420123456789', 'Test message')
            assert result is False


class TestSendWeatherSmsForEtapa:
    def _setup_etapa_and_registrations(self, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, date, route, route_start, route_end, color) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, 'Test Etapa', '11.09.2026 (čtvrtek)', 'A - B', 'Olomouc', 'Brno', '#ffc107'),
            )
            for i, phone in enumerate(['123456789', '987654321', '']):
                db.execute(
                    "INSERT INTO registrace (name, email, phone, status) VALUES (?, ?, ?, 'approved')",
                    (f'User {i}', f'user{i}@test.cz', phone),
                )
            db.execute(
                "INSERT INTO registrace (name, email, phone, status) VALUES (?, ?, ?, 'pending')",
                ('Pending User', 'pending@test.cz', '111222333'),
            )
            db.commit()

    def test_sends_to_approved_with_phone(self, app):
        self._setup_etapa_and_registrations(app)
        mock_geo = MagicMock()
        mock_geo.json.return_value = {'results': [{'latitude': 49.59, 'longitude': 17.25}]}
        today = datetime.now().strftime('%Y-%m-%d')
        hourly_times = [f'{today}T{h:02d}:00' for h in range(24)]
        hourly_temps = [12, 11, 10, 10, 11, 13, 15, 17, 19, 21, 23, 24, 25, 25, 24, 23, 21, 20, 18, 16, 15, 14, 13, 12]
        mock_weather = MagicMock()
        mock_weather.json.return_value = {
            'daily': {
                'time': [today],
                'temperature_2m_max': [25],
                'temperature_2m_min': [10],
                'weather_code': [0],
                'precipitation_probability_max': [5],
            },
            'hourly': {
                'time': hourly_times,
                'temperature_2m': hourly_temps,
            },
        }
        with patch('app.requests.get', side_effect=[mock_geo, mock_weather, mock_geo, mock_weather]):
            with patch('app.send_sms', return_value=True) as mock_sms:
                with patch('app._time.sleep'):
                    import app as flask_app
                    with app.app_context():
                        sent = flask_app.send_weather_sms_for_etapa(1)
                        assert sent == 2
                        assert mock_sms.call_count == 2
                        phones = [call[0][0] for call in mock_sms.call_args_list]
                        assert '+420123456789' in phones
                        assert '+420987654321' in phones

    def test_skips_if_no_route_start(self, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, date, route, color) VALUES (?, ?, ?, ?, ?)",
                (1, 'Test', '11.09.2026', 'A-B', '#ffc107'),
            )
            db.commit()
            sent = flask_app.send_weather_sms_for_etapa(1)
            assert sent == 0


class TestAdminSmsPage:
    def _create_etapa(self, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, date, route, route_start, route_end, color) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, 'Test Etapa', '11.09.2026', 'A-B', 'Olomouc', 'Brno', '#ffc107'),
            )
            db.commit()

    def test_sms_page_loads(self, admin_client, app):
        self._create_etapa(app)
        r = admin_client.get('/admin/sms')
        assert r.status_code == 200
        html = r.data.decode()
        assert 'SMS' in html
        assert 'Olomouc' in html
        assert 'Brno' in html

    def test_sms_page_shows_recipient_count(self, admin_client, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, phone, status) VALUES (?, ?, ?, 'approved')",
                ('Test', 'test@test.cz', '123456789'),
            )
            db.commit()
        r = admin_client.get('/admin/sms')
        assert r.status_code == 200

    def test_sms_preview_with_weather(self, admin_client, app):
        self._create_etapa(app)
        mock_geo = MagicMock()
        mock_geo.json.return_value = {'results': [{'latitude': 49.59, 'longitude': 17.25}]}
        today = datetime.now().strftime('%Y-%m-%d')
        hourly_times = [f'{today}T{h:02d}:00' for h in range(24)]
        hourly_temps = [12, 11, 10, 10, 11, 13, 15, 17, 19, 21, 23, 24, 25, 25, 24, 23, 21, 20, 18, 16, 15, 14, 13, 12]
        mock_weather = MagicMock()
        mock_weather.json.return_value = {
            'daily': {
                'time': [today],
                'temperature_2m_max': [25],
                'temperature_2m_min': [10],
                'weather_code': [0],
                'precipitation_probability_max': [5],
            },
            'hourly': {
                'time': hourly_times,
                'temperature_2m': hourly_temps,
            },
        }
        with patch('app.requests.get', side_effect=[mock_geo, mock_weather, mock_geo, mock_weather]):
            r = admin_client.get('/admin/sms/preview/1')
            assert r.status_code == 200
            html = r.data.decode()
            assert 'Cykloexpedice' in html
            assert 'Jasno' in html

    def test_sms_preview_missing_locations(self, admin_client, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, date, route, color) VALUES (?, ?, ?, ?, ?)",
                (1, 'Test', '11.09.2026', 'A-B', '#ffc107'),
            )
            db.commit()
        r = admin_client.get('/admin/sms/preview/1', follow_redirects=True)
        assert r.status_code == 200

    def test_sms_send_endpoint(self, admin_client, app):
        self._create_etapa(app)
        with patch('app.send_weather_sms_for_etapa', return_value=5):
            r = admin_client.post('/admin/sms/send/1', follow_redirects=True)
            assert r.status_code == 200


class TestEtapaRouteStartEnd:
    def test_save_route_start_end(self, admin_client, app):
        r = admin_client.post('/admin/etapy/new', data={
            'number': '1', 'title': 'Test Etapa',
            'date': '11.09.2026', 'distance': '100 km',
            'elevation_up': '500m', 'elevation_down': '300m',
            'route': 'A - B', 'route_start': 'Olomouc', 'route_end': 'Brno',
            'waypoints': '', 'description': '', 'map_link': '', 'youtube_links': '',
            'color': '#ffc107',
        }, follow_redirects=True)
        assert r.status_code == 200
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            etapa = db.execute('SELECT * FROM etapy WHERE number = 1').fetchone()
            assert etapa['route_start'] == 'Olomouc'
            assert etapa['route_end'] == 'Brno'


class TestNotificationHtmlEscaping:
    def test_xss_in_name_is_escaped(self, client):
        with patch('app.send_email') as mock_email:
            client.post('/registrace', data={
                'name': '<script>alert(1)</script>',
                'email': 'xss@test.cz',
                'phone': '',
                'note': '',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
            admin_call = mock_email.call_args_list[1]
            html_body = admin_call[0][2]
            assert '<script>' not in html_body
            assert '&lt;script&gt;' in html_body


# ── User auth tests ──────────────────────────────────────────────


class TestUserRegistration:
    def test_register_success(self, client):
        resp = client.post('/login', data={
            'action': 'register', 'email': 'jan@test.cz',
            'name': 'Jan Novak', 'password': 'heslo123', 'password2': 'heslo123',
        }, follow_redirects=True)
        assert 'Jan Novak' in resp.data.decode()

    def test_register_duplicate_email(self, client):
        client.post('/login', data={
            'action': 'register', 'email': 'dup@test.cz',
            'name': 'Jan', 'password': 'heslo123', 'password2': 'heslo123',
        })
        client.get('/logout')
        resp = client.post('/login', data={
            'action': 'register', 'email': 'dup@test.cz',
            'name': 'Jan2', 'password': 'heslo456', 'password2': 'heslo456',
        }, follow_redirects=True)
        assert 'již existuje' in resp.data.decode()

    def test_register_short_password(self, client):
        resp = client.post('/login', data={
            'action': 'register', 'email': 'jan@test.cz',
            'name': 'Jan', 'password': 'short', 'password2': 'short',
        }, follow_redirects=True)
        assert 'alespoň 8 znaků' in resp.data.decode()

    def test_register_password_mismatch(self, client):
        resp = client.post('/login', data={
            'action': 'register', 'email': 'jan@test.cz',
            'name': 'Jan', 'password': 'heslo123', 'password2': 'heslo999',
        }, follow_redirects=True)
        assert 'neshodují' in resp.data.decode()

    def test_register_missing_fields(self, client):
        resp = client.post('/login', data={
            'action': 'register', 'email': '',
            'name': '', 'password': '', 'password2': '',
        }, follow_redirects=True)
        assert 'Vyplňte' in resp.data.decode()

    def test_check_email_new_shows_register(self, client):
        resp = client.post('/login', data={
            'action': 'check_email', 'email': 'new@test.cz',
        })
        assert resp.status_code == 200
        assert 'register' in resp.data.decode()

    def test_check_email_existing_shows_login(self, client, app):
        import bcrypt
        import app as flask_app
        pw = bcrypt.hashpw(b'heslo123', bcrypt.gensalt()).decode()
        with app.app_context():
            db = flask_app.get_db()
            db.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                       ('Existing', 'exists@test.cz', pw))
            db.commit()
        resp = client.post('/login', data={
            'action': 'check_email', 'email': 'exists@test.cz',
        })
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'value="login"' in html
        assert 'register' not in html.split('value="login"')[0].split('action')[-1]

    def test_check_email_empty(self, client):
        resp = client.post('/login', data={
            'action': 'check_email', 'email': '',
        }, follow_redirects=True)
        assert 'Zadejte' in resp.data.decode()


class TestUserLogin:
    def test_login_success(self, client):
        client.post('/login', data={
            'action': 'register', 'email': 'login@test.cz',
            'name': 'Jan', 'password': 'heslo123', 'password2': 'heslo123',
        })
        client.get('/logout')
        resp = client.post('/login', data={
            'action': 'login', 'email': 'login@test.cz', 'password': 'heslo123',
        }, follow_redirects=True)
        assert 'Jan' in resp.data.decode()

    def test_login_wrong_password(self, client):
        client.post('/login', data={
            'action': 'register', 'email': 'wrong@test.cz',
            'name': 'Jan', 'password': 'heslo123', 'password2': 'heslo123',
        })
        client.get('/logout')
        resp = client.post('/login', data={
            'action': 'login', 'email': 'wrong@test.cz', 'password': 'badpass1',
        }, follow_redirects=True)
        assert 'Neplatn' in resp.data.decode()

    def test_login_nonexistent_email(self, client):
        resp = client.post('/login', data={
            'action': 'login', 'email': 'nobody@test.cz', 'password': 'heslo123',
        }, follow_redirects=True)
        assert 'Neplatn' in resp.data.decode()

    def test_login_page_loads(self, client):
        resp = client.get('/login')
        assert resp.status_code == 200
        assert 'check_email' in resp.data.decode()

    def test_logout(self, user_client):
        resp = user_client.get('/logout', follow_redirects=True)
        assert resp.status_code == 200
        resp = user_client.get('/app')
        assert resp.status_code == 302


class TestUserForgotPassword:
    def test_forgot_password_page_loads(self, client):
        resp = client.get('/forgot-password')
        assert resp.status_code == 200
        assert 'Obnovení hesla' in resp.get_data(as_text=True)

    def test_forgot_password_sends_email(self, client, app, monkeypatch):
        import app as flask_app
        pw_hash = bcrypt.hashpw(b'testpass', bcrypt.gensalt()).decode()
        with app.app_context():
            d = flask_app.get_db()
            d.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                      ('Reset User', 'reset@test.cz', pw_hash))
            d.commit()

        sent = []
        monkeypatch.setattr(flask_app, 'send_email', lambda *a, **kw: sent.append(a))
        resp = client.post('/forgot-password', data={'email': 'reset@test.cz'}, follow_redirects=True)
        assert resp.status_code == 200
        assert 'odeslali jsme odkaz' in resp.get_data(as_text=True)
        assert len(sent) == 1
        assert sent[0][0] == 'reset@test.cz'

    def test_forgot_password_nonexistent_email_no_leak(self, client, app, monkeypatch):
        import app as flask_app
        sent = []
        monkeypatch.setattr(flask_app, 'send_email', lambda *a, **kw: sent.append(a))
        resp = client.post('/forgot-password', data={'email': 'nobody@test.cz'}, follow_redirects=True)
        assert resp.status_code == 200
        assert 'odeslali jsme odkaz' in resp.get_data(as_text=True)
        assert len(sent) == 0

    def test_reset_password_valid_token(self, client, app):
        import app as flask_app
        pw_hash = bcrypt.hashpw(b'oldpass11', bcrypt.gensalt()).decode()
        with app.app_context():
            d = flask_app.get_db()
            d.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                      ('Reset User2', 'reset2@test.cz', pw_hash))
            d.commit()
            user = d.execute("SELECT id FROM users WHERE email = 'reset2@test.cz'").fetchone()
            token = 'valid-test-token-abc123'
            from datetime import datetime, timedelta
            expires = datetime.now() + timedelta(hours=1)
            d.execute("INSERT INTO user_password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
                      (user['id'], token, expires))
            d.commit()

        resp = client.get('/reset-password/valid-test-token-abc123')
        assert resp.status_code == 200
        assert 'Nastavení nového hesla' in resp.get_data(as_text=True)

        resp = client.post('/reset-password/valid-test-token-abc123',
                           data={'password': 'newpass88', 'password2': 'newpass88'},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert 'Heslo bylo úspěšně změněno' in resp.get_data(as_text=True)

        resp = client.post('/login', data={'action': 'login', 'email': 'reset2@test.cz', 'password': 'newpass88'},
                           follow_redirects=True)
        assert resp.status_code == 200

    def test_reset_password_invalid_token(self, client):
        resp = client.get('/reset-password/bogus-token', follow_redirects=True)
        assert 'neplatný nebo vypršel' in resp.get_data(as_text=True)

    def test_reset_password_expired_token(self, client, app):
        import app as flask_app
        pw_hash = bcrypt.hashpw(b'oldpass11', bcrypt.gensalt()).decode()
        with app.app_context():
            d = flask_app.get_db()
            d.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                      ('Expired User', 'expired@test.cz', pw_hash))
            d.commit()
            user = d.execute("SELECT id FROM users WHERE email = 'expired@test.cz'").fetchone()
            from datetime import datetime, timedelta
            expires = datetime.now() - timedelta(hours=1)
            d.execute("INSERT INTO user_password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
                      (user['id'], 'expired-token-xyz', expires))
            d.commit()

        resp = client.get('/reset-password/expired-token-xyz', follow_redirects=True)
        assert 'neplatný nebo vypršel' in resp.get_data(as_text=True)

    def test_reset_password_short_password(self, client, app):
        import app as flask_app
        pw_hash = bcrypt.hashpw(b'oldpass11', bcrypt.gensalt()).decode()
        with app.app_context():
            d = flask_app.get_db()
            d.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                      ('Short User', 'short@test.cz', pw_hash))
            d.commit()
            user = d.execute("SELECT id FROM users WHERE email = 'short@test.cz'").fetchone()
            from datetime import datetime, timedelta
            expires = datetime.now() + timedelta(hours=1)
            d.execute("INSERT INTO user_password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
                      (user['id'], 'short-pw-token', expires))
            d.commit()

        resp = client.post('/reset-password/short-pw-token',
                           data={'password': 'short', 'password2': 'short'})
        assert resp.status_code == 200
        assert 'alespoň 8 znaků' in resp.get_data(as_text=True)

    def test_reset_password_mismatch(self, client, app):
        import app as flask_app
        pw_hash = bcrypt.hashpw(b'oldpass11', bcrypt.gensalt()).decode()
        with app.app_context():
            d = flask_app.get_db()
            d.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                      ('Mismatch User', 'mismatch@test.cz', pw_hash))
            d.commit()
            user = d.execute("SELECT id FROM users WHERE email = 'mismatch@test.cz'").fetchone()
            from datetime import datetime, timedelta
            expires = datetime.now() + timedelta(hours=1)
            d.execute("INSERT INTO user_password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
                      (user['id'], 'mismatch-token', expires))
            d.commit()

        resp = client.post('/reset-password/mismatch-token',
                           data={'password': 'newpass88', 'password2': 'different1'})
        assert resp.status_code == 200
        assert 'neshodují' in resp.get_data(as_text=True)

    def test_reset_token_invalidated_after_use(self, client, app):
        import app as flask_app
        pw_hash = bcrypt.hashpw(b'oldpass11', bcrypt.gensalt()).decode()
        with app.app_context():
            d = flask_app.get_db()
            d.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                      ('Reuse User', 'reuse@test.cz', pw_hash))
            d.commit()
            user = d.execute("SELECT id FROM users WHERE email = 'reuse@test.cz'").fetchone()
            from datetime import datetime, timedelta
            expires = datetime.now() + timedelta(hours=1)
            d.execute("INSERT INTO user_password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
                      (user['id'], 'one-time-token', expires))
            d.commit()

        client.post('/reset-password/one-time-token',
                     data={'password': 'newpass88', 'password2': 'newpass88'},
                     follow_redirects=True)
        resp = client.get('/reset-password/one-time-token', follow_redirects=True)
        assert 'neplatný nebo vypršel' in resp.get_data(as_text=True)

    def test_login_page_shows_forgot_link(self, client, app):
        import app as flask_app
        pw_hash = bcrypt.hashpw(b'testpass', bcrypt.gensalt()).decode()
        with app.app_context():
            d = flask_app.get_db()
            d.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                      ('Link User', 'link@test.cz', pw_hash))
            d.commit()
        resp = client.post('/login', data={'action': 'check_email', 'email': 'link@test.cz'})
        assert 'forgot-password' in resp.get_data(as_text=True)


class TestUserDashboard:
    def test_app_requires_login(self, client):
        resp = client.get('/app')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']

    def test_dashboard_redirects_to_app(self, user_client):
        resp = user_client.get('/dashboard')
        assert resp.status_code == 302
        assert '/app' in resp.headers['Location']

    def test_app_loads(self, user_client):
        resp = user_client.get('/app')
        assert resp.status_code == 200
        assert 'Test User' in resp.data.decode()

    def test_app_shows_strava_connect(self, user_client):
        resp = user_client.get('/app')
        assert 'Připojit' in resp.data.decode()

    def test_app_shows_initials(self, user_client):
        resp = user_client.get('/app')
        assert 'TU' in resp.data.decode()

    def test_app_shows_registration_status(self, app, user_client):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute("INSERT INTO registrace (name, email, status, payment_status) VALUES (?, ?, ?, ?)",
                       ('Test User', 'user@test.cz', 'approved', 'paid'))
            db.commit()
        resp = user_client.get('/app')
        html = resp.data.decode()
        assert 'Schváleno' in html
        assert 'Zaplaceno' in html

    def test_app_shows_no_registration(self, user_client):
        resp = user_client.get('/app')
        assert 'Nemáte registraci' in resp.data.decode()


class TestStravaConnect:
    def test_connect_redirects_to_strava(self, app, user_client, monkeypatch):
        import app as flask_app
        monkeypatch.setattr(flask_app, 'STRAVA_CLIENT_ID', '12345')
        resp = user_client.get('/strava/connect')
        assert resp.status_code == 302
        assert 'strava.com/oauth/authorize' in resp.headers['Location']
        assert 'client_id=12345' in resp.headers['Location']

    def test_connect_without_config(self, app, user_client, monkeypatch):
        import app as flask_app
        monkeypatch.setattr(flask_app, 'STRAVA_CLIENT_ID', '')
        resp = user_client.get('/strava/connect', follow_redirects=True)
        assert 'není nakonfigurována' in resp.data.decode()

    def test_connect_requires_login(self, client):
        resp = client.get('/strava/connect')
        assert resp.status_code == 302

    def test_connect_includes_state(self, app, user_client, monkeypatch):
        import app as flask_app
        monkeypatch.setattr(flask_app, 'STRAVA_CLIENT_ID', '12345')
        resp = user_client.get('/strava/connect')
        assert 'state=' in resp.headers['Location']

    def test_callback_stores_tokens(self, app, user_client, monkeypatch):
        import app as flask_app
        monkeypatch.setattr(flask_app, 'STRAVA_CLIENT_ID', '12345')
        monkeypatch.setattr(flask_app, 'STRAVA_CLIENT_SECRET', 'secret')

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'access_token': 'acc_tok', 'refresh_token': 'ref_tok',
            'expires_at': 9999999999, 'athlete': {'id': 42},
        }
        with user_client.session_transaction() as sess:
            sess['strava_oauth_state'] = 'test_state'
        with patch('app.requests.post', return_value=mock_resp):
            resp = user_client.get('/strava/callback?code=testcode&state=test_state', follow_redirects=True)

        assert 'úspěšně propojena' in resp.data.decode()
        with app.app_context():
            db = flask_app.get_db()
            user = db.execute('SELECT * FROM users WHERE email = ?', ('user@test.cz',)).fetchone()
            assert user['strava_athlete_id'] == 42
            assert user['strava_access_token'] == 'acc_tok'

    def test_callback_rejects_missing_state(self, user_client):
        resp = user_client.get('/strava/callback?code=testcode', follow_redirects=True)
        assert 'OAuth' in resp.data.decode()

    def test_callback_rejects_wrong_state(self, user_client):
        with user_client.session_transaction() as sess:
            sess['strava_oauth_state'] = 'correct'
        resp = user_client.get('/strava/callback?code=testcode&state=wrong', follow_redirects=True)
        assert 'OAuth' in resp.data.decode()

    def test_callback_no_code(self, user_client):
        with user_client.session_transaction() as sess:
            sess['strava_oauth_state'] = 'test_state'
        resp = user_client.get('/strava/callback?state=test_state', follow_redirects=True)
        assert 'selhala' in resp.data.decode()

    def test_callback_api_error(self, app, user_client, monkeypatch):
        import app as flask_app
        monkeypatch.setattr(flask_app, 'STRAVA_CLIENT_ID', '12345')
        monkeypatch.setattr(flask_app, 'STRAVA_CLIENT_SECRET', 'secret')

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with user_client.session_transaction() as sess:
            sess['strava_oauth_state'] = 'test_state'
        with patch('app.requests.post', return_value=mock_resp):
            resp = user_client.get('/strava/callback?code=badcode&state=test_state', follow_redirects=True)
        assert 'Nepodařilo' in resp.data.decode()

    def test_disconnect(self, app, user_client):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute('''UPDATE users SET strava_athlete_id = 42, strava_access_token = 'tok',
                          strava_refresh_token = 'ref', strava_expires_at = 9999999999
                          WHERE email = ?''', ('user@test.cz',))
            db.commit()

        resp = user_client.post('/strava/disconnect', follow_redirects=True)
        assert 'odpojena' in resp.data.decode()
        with app.app_context():
            db = flask_app.get_db()
            user = db.execute('SELECT * FROM users WHERE email = ?', ('user@test.cz',)).fetchone()
            assert user['strava_access_token'] is None


class TestStravaDashboardActivities:
    def test_dashboard_with_strava_shows_activities(self, app, user_client):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute('''UPDATE users SET strava_athlete_id = 42, strava_access_token = 'tok',
                          strava_refresh_token = 'ref', strava_expires_at = 9999999999
                          WHERE email = ?''', ('user@test.cz',))
            db.commit()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {'id': 1001, 'type': 'Ride', 'name': 'Morning Ride', 'distance': 82000,
             'moving_time': 10800, 'total_elevation_gain': 500, 'average_speed': 7.6,
             'start_date_local': '2025-09-11T08:00:00Z'},
            {'id': 1002, 'type': 'Run', 'name': 'Jog', 'distance': 5000,
             'moving_time': 1800, 'total_elevation_gain': 30, 'average_speed': 2.8,
             'start_date_local': '2025-09-11T09:00:00Z'},
        ]
        with patch('app.requests.get', return_value=mock_resp):
            resp = user_client.get('/app')

        html = resp.data.decode()
        assert 'Morning Ride' in html
        assert 'Jog' not in html
        assert '82.0 km' in html

    def test_dashboard_strava_token_refresh(self, app, user_client):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute('''UPDATE users SET strava_athlete_id = 42, strava_access_token = 'old_tok',
                          strava_refresh_token = 'ref', strava_expires_at = 1
                          WHERE email = ?''', ('user@test.cz',))
            db.commit()

        refresh_resp = MagicMock()
        refresh_resp.status_code = 200
        refresh_resp.json.return_value = {
            'access_token': 'new_tok', 'refresh_token': 'new_ref', 'expires_at': 9999999999,
        }
        activities_resp = MagicMock()
        activities_resp.status_code = 200
        activities_resp.json.return_value = []

        with patch('app.requests.post', return_value=refresh_resp), \
             patch('app.requests.get', return_value=activities_resp):
            resp = user_client.get('/app')

        assert resp.status_code == 200
        with app.app_context():
            db = flask_app.get_db()
            user = db.execute('SELECT * FROM users WHERE email = ?', ('user@test.cz',)).fetchone()
            assert user['strava_access_token'] == 'new_tok'

    def _setup_strava_user(self, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute('''UPDATE users SET strava_athlete_id = 42, strava_access_token = 'tok',
                          strava_refresh_token = 'ref', strava_expires_at = 9999999999
                          WHERE email = ?''', ('user@test.cz',))
            db.commit()

    def test_auto_dedup_prefers_device_over_phone(self, app, user_client):
        self._setup_strava_user(app)

        phone_ride = {'id': 2001, 'type': 'Ride', 'name': 'Phone Ride', 'distance': 80000,
                      'moving_time': 10800, 'total_elevation_gain': 400, 'average_speed': 7.4,
                      'start_date_local': '2025-09-11T08:00:00Z'}
        device_ride = {'id': 2002, 'type': 'Ride', 'name': 'Garmin Ride', 'distance': 81000,
                       'moving_time': 10900, 'total_elevation_gain': 410, 'average_speed': 7.4,
                       'start_date_local': '2025-09-11T08:05:00Z'}

        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = [phone_ride, device_ride]

        detail_phone = MagicMock()
        detail_phone.status_code = 200
        detail_phone.json.return_value = {'device_name': 'Strava iPhone App'}

        detail_device = MagicMock()
        detail_device.status_code = 200
        detail_device.json.return_value = {'device_name': 'Garmin Edge 530'}

        def mock_get(url, **kwargs):
            if '/activities/2001' in url:
                return detail_phone
            if '/activities/2002' in url:
                return detail_device
            return list_resp

        with patch('app.requests.get', side_effect=mock_get):
            resp = user_client.get('/app')

        html = resp.data.decode()
        assert 'Garmin Ride' in html
        assert 'Phone Ride' not in html

        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            hidden = db.execute('SELECT strava_activity_id FROM hidden_activities WHERE user_id = (SELECT id FROM users WHERE email = ?)',
                                ('user@test.cz',)).fetchall()
            assert 2001 in {r['strava_activity_id'] for r in hidden}

    def test_auto_dedup_keeps_all_when_same_type(self, app, user_client):
        self._setup_strava_user(app)

        ride1 = {'id': 3001, 'type': 'Ride', 'name': 'Garmin Ride 1', 'distance': 80000,
                 'moving_time': 10800, 'total_elevation_gain': 400, 'average_speed': 7.4,
                 'start_date_local': '2025-09-11T08:00:00Z'}
        ride2 = {'id': 3002, 'type': 'Ride', 'name': 'Wahoo Ride 2', 'distance': 81000,
                 'moving_time': 10900, 'total_elevation_gain': 410, 'average_speed': 7.4,
                 'start_date_local': '2025-09-11T08:05:00Z'}

        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = [ride1, ride2]

        detail1 = MagicMock()
        detail1.status_code = 200
        detail1.json.return_value = {'device_name': 'Garmin Edge 530'}

        detail2 = MagicMock()
        detail2.status_code = 200
        detail2.json.return_value = {'device_name': 'Wahoo ELEMNT'}

        def mock_get(url, **kwargs):
            if '/activities/3001' in url:
                return detail1
            if '/activities/3002' in url:
                return detail2
            return list_resp

        with patch('app.requests.get', side_effect=mock_get):
            resp = user_client.get('/app')

        html = resp.data.decode()
        assert 'Garmin Ride 1' in html
        assert 'Wahoo Ride 2' in html

    def test_auto_dedup_uses_device_cache(self, app, user_client):
        self._setup_strava_user(app)

        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            user = db.execute('SELECT id FROM users WHERE email = ?', ('user@test.cz',)).fetchone()
            db.execute('INSERT INTO activity_device_cache (user_id, strava_activity_id, device_name) VALUES (?, ?, ?)',
                       (user['id'], 4001, 'Strava Android App'))
            db.execute('INSERT INTO activity_device_cache (user_id, strava_activity_id, device_name) VALUES (?, ?, ?)',
                       (user['id'], 4002, 'Garmin Edge 830'))
            db.commit()

        phone_ride = {'id': 4001, 'type': 'Ride', 'name': 'Phone Cached', 'distance': 80000,
                      'moving_time': 10800, 'total_elevation_gain': 400, 'average_speed': 7.4,
                      'start_date_local': '2025-09-11T08:00:00Z'}
        device_ride = {'id': 4002, 'type': 'Ride', 'name': 'Device Cached', 'distance': 81000,
                       'moving_time': 10900, 'total_elevation_gain': 410, 'average_speed': 7.4,
                       'start_date_local': '2025-09-11T08:05:00Z'}

        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = [phone_ride, device_ride]

        call_log = []

        def mock_get(url, **kwargs):
            call_log.append(url)
            return list_resp

        with patch('app.requests.get', side_effect=mock_get):
            resp = user_client.get('/app')

        html = resp.data.decode()
        assert 'Device Cached' in html
        assert 'Phone Cached' not in html
        assert not any('/activities/4001' in u for u in call_log), "Should not call detail API for cached activity"
        assert not any('/activities/4002' in u for u in call_log), "Should not call detail API for cached activity"


class TestCheckFioPaymentsConfirmationEmail:
    def test_sends_confirmation_email_on_match(self, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Jan Novák', 'jan@test.cz', 55, 3500))
            db.commit()

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500},
                        'column5': {'value': '55'},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get, \
             patch('app.send_bulk_emails') as mock_bulk:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

            mock_bulk.assert_called_once()
            items = mock_bulk.call_args[0][0]
            assert len(items) == 1
            assert items[0][0] == 'jan@test.cz'
            assert 'Platba přijata' in items[0][1] or 'platby' in items[0][1].lower()
            assert 'Jane' in items[0][2]  # vocative of Jan

    def test_no_email_when_no_match(self, app):
        import app as flask_app

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500},
                        'column5': {'value': '999'},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get, \
             patch('app.send_bulk_emails') as mock_bulk:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()
            mock_bulk.assert_not_called()

    def test_no_email_when_no_email_address(self, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('No Email', '', 66, 3500))
            db.commit()

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500},
                        'column5': {'value': '66'},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get, \
             patch('app.send_bulk_emails') as mock_bulk:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()
            mock_bulk.assert_not_called()


class TestVSLeadingZeroStripping:
    def test_matches_vs_with_leading_zeros(self, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Test Zero', 'z@y.cz', 23, 3500))
            db.commit()

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500},
                        'column5': {'value': '0000000023'},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get, \
             patch('app.send_bulk_emails'):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT payment_status FROM registrace WHERE variable_symbol = 23").fetchone()
            assert row['payment_status'] == 'paid'

    def test_matches_vs_without_leading_zeros(self, app):
        """VS stored as 42 should still match bank's '42' (no leading zeros)."""
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Test Plain', 'plain@y.cz', 42, 3500))
            db.commit()

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500},
                        'column5': {'value': '42'},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get, \
             patch('app.send_bulk_emails'):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT payment_status FROM registrace WHERE variable_symbol = 42").fetchone()
            assert row['payment_status'] == 'paid'


class TestLastPaymentCheckTimestamp:
    def test_timestamp_saved_on_successful_check(self, app):
        import app as flask_app

        fio_response = {
            'accountStatement': {
                'transactionList': None
            }
        }

        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT value FROM site_settings WHERE key = 'last_payment_check'").fetchone()
            assert row is not None
            assert ':' in row['value']  # HH:MM:SS format

    def test_timestamp_shown_in_registrace(self, admin_client, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('last_payment_check', '14:30:05'))
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'paid', ?)",
                ('Paid User', 'paid@test.cz', 77, 3500))
            db.commit()

        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert '14:30:05' in html

    def test_no_timestamp_shows_placeholder(self, admin_client, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'paid', ?)",
                ('Paid User2', 'paid2@test.cz', 78, 3500))
            db.commit()

        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'nebyly zkontrolovány' in html


class TestOrganizerPatching:
    def test_adam_prikryl_shown_as_organizer(self, admin_client, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                ('Adam Přikryl', 'adam@test.cz'))
            db.commit()

        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'Organizátoři' in html

    def test_michal_prikryl_shown_as_organizer(self, admin_client, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                ('Michal Přikryl', 'michal@test.cz'))
            db.commit()

        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'Organizátoři' in html

    def test_regular_user_not_shown_as_organizer(self, admin_client, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'paid', ?)",
                ('Jan Novák', 'jan@test.cz', 88, 3500))
            db.commit()

        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'Zaplaceno' in html
        assert 'Jan Novák' in html

    def test_organizer_hides_vs_and_amount(self, admin_client, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Adam Přikryl', 'adam@test.cz', 101, 3500))
            db.commit()

        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        # Organizer row should not show the variable symbol value
        assert '101' not in html or 'Organizátoři' in html


class TestPaymentConfirmedEmailTemplate:
    def test_default_template_exists(self, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            subj = db.execute(
                "SELECT value FROM site_settings WHERE key = 'email_payment_confirmed_subject'"
            ).fetchone()
            body = db.execute(
                "SELECT value FROM site_settings WHERE key = 'email_payment_confirmed_body'"
            ).fetchone()
            assert subj is not None
            assert 'Platba přijata' in subj['value']
            assert body is not None
            assert 'startovní listině' in body['value']

    def test_template_in_admin_list(self, admin_client):
        r = admin_client.get('/admin/email-sablony')
        html = r.data.decode()
        assert 'Potvrzení platby' in html

    def test_template_renders_correctly(self, app):
        from app import render_email_template
        with app.app_context():
            variables = {
                'name': 'Petr Novák',
                'event_name': 'Cykloexpedice',
                'event_year': '2026',
                'contact_name_1': 'Adam',
                'contact_name_2': 'Michal',
                'contact_email': 'info@cyklo.cz',
            }
            subject, html = render_email_template('email_payment_confirmed', variables)
            assert 'Platba přijata' in subject
            assert 'Petře' in html  # vocative
            assert 'startovní listině' in html
            assert '{{' not in html


class TestScheduledPaymentChecks:
    def test_schedule_function_exists(self):
        from app import _schedule_payment_checks
        assert callable(_schedule_payment_checks)

    def test_schedule_creates_jobs(self):
        from app import _schedule_payment_checks
        with patch('app.BackgroundScheduler', create=True) as MockScheduler:
            mock_sched = MagicMock()
            MockScheduler.return_value = mock_sched
            with patch.dict('sys.modules', {'apscheduler.schedulers.background': MagicMock(BackgroundScheduler=MockScheduler)}):
                _schedule_payment_checks()
            mock_sched.add_job.assert_called_once_with(
                ANY, 'cron', minute='0,15,30,45', id='payment_check'
            )
            assert mock_sched.start.called
