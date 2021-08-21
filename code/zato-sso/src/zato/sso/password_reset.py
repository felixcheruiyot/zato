# -*- coding: utf-8 -*-

"""
Copyright (C) 2021, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
from contextlib import closing
from datetime import datetime, timedelta
from logging import getLogger

# Bunch
from bunch import Bunch

# SQLAlchemy
from sqlalchemy import and_

# Zato
from zato.common import GENERIC, SMTPMessage
from zato.common.api import SSO as CommonSSO
from zato.common.json_internal import json_dumps
from zato.common.odb.model import SSOPasswordReset as FlowPRTModel
from zato.sso import const, Default, status_code, ValidationError
from zato.sso.common import SSOCtx
from zato.sso.odb.query import get_user_by_email, get_user_by_name, get_user_by_name_or_email, get_user_by_prt, \
     get_user_by_prt_and_reset_key
from zato.sso.util import new_prt, new_prt_reset_key, UserChecker

# ################################################################################################################################

if 0:
    from typing import Callable
    from zato.common.odb.model import SSOUser
    from zato.server.base.parallel import ParallelServer
    from zato.server.connection.email import SMTPConnection

    Callable = Callable
    ParallelServer = ParallelServer
    SMTPConnection = SMTPConnection
    SSOUser = SSOUser

# ################################################################################################################################

logger = getLogger('zato')

# ################################################################################################################################
# ################################################################################################################################

FlowPRTModelTable = FlowPRTModel.__table__
FlowPRTModelInsert = FlowPRTModelTable.insert
FlowPRTModelUpdate = FlowPRTModelTable.update

# ################################################################################################################################
# ################################################################################################################################

_unrecognised_locale = object()

# ################################################################################################################################
# ################################################################################################################################

# Maps configuration keys to functions that look up users in the database.
user_search_by_map = {
    'username':          get_user_by_name,
    'email':             get_user_by_email,
    'username_or_email': get_user_by_name_or_email,
}

# ################################################################################################################################
# ################################################################################################################################

class PasswordResetAPI(object):
    """ Message flow around password-reset tokens (PRT).
    """
    def __init__(self, server, sso_conf, odb_session_func, decrypt_func, verify_hash_func):
        # type: (ParallelServer, dict, Callable, Callable, Callable) -> None
        self.server = server
        self.sso_conf = sso_conf
        self.odb_session_func = odb_session_func
        self.decrypt_func = decrypt_func
        self.verify_hash_func = verify_hash_func
        self.is_sqlite = None

        # PRT runtime configuration
        prt_config = sso_conf.get('prt', {}) # type: dict

        # For how long PRTs are valid (in seconds)
        valid_for = prt_config.get('valid_for')
        valid_for = valid_for or Default.prt_valid_for
        self.valid_for = int(valid_for)

        # For how long the one-off session to change the password will last (in minutes)
        duration = prt_config.get('password_change_session_duration')
        duration = duration or Default.prt_password_change_session_duration
        self.password_change_session_duration = int(duration)

        # By what credential to look up users in the database
        user_search_by = prt_config.get('user_search_by')
        user_search_by = user_search_by or Default.prt_user_search_by
        self.user_search_by_func = user_search_by_map[user_search_by]

        # Checks user context when a PRT is being accessed
        self.user_checker = UserChecker(self.decrypt_func, self.verify_hash_func, self.sso_conf)

        # Name of the site, e.g. the environemnt the SSO servers in
        self.site_name = self.sso_conf.main.get('site_name') or 'site'

        # Convert minutes to hours as it is hours that are sent in notification emails
        self.expiration_time_hours = int(self.sso_conf.password_reset.valid_for) // 60

        # Name of an outgoing SMTP connections to send notifications through
        self.smtp_conn_name = sso_conf.main.smtp_conn # type: str

        # From who the SMTP messages will be sent
        self.email_from = sso_conf.password_reset.email_from

# ################################################################################################################################

    def post_configure(self, func, is_sqlite):
        # type: (Callable, bool) -> None
        self.odb_session_func = func
        self.is_sqlite = is_sqlite

# ################################################################################################################################

    def create_token(self, cid, credential, current_app, remote_addr, user_agent, _utcnow=datetime.utcnow, _timedelta=timedelta):
        # type: (str, object, object) -> None

        # Validate input
        if not credential:
            logger.warn('SSO credential missing on input to PasswordResetAPI.create_token (%r)', credential)
            return

        # For later use
        sso_ctx = self._build_sso_ctx(cid, remote_addr, user_agent, current_app)

        # Look up the user in the database ..
        with closing(self.odb_session_func()) as session:
            user = self.user_search_by_func(session, credential) # type: SSOUser

            # .. make sure the user exists ..
            if not user:
                logger.warn('No such SSO user `%s` (%s)', credential, self.user_search_by_func)
                return

            # .. the user exists so we can now generate a new PRT ..
            prt = new_prt()

            # .. timestamp metadata ..
            creation_time = _utcnow()
            expiration_time = creation_time + timedelta(minutes=self.valid_for)

            # .. these are the same so be explicit about it ..
            reset_key_exp_time = expiration_time

            # .. reset key used along with the PRT to reset the password ..
            reset_key = new_prt_reset_key()

            # .. insert it into the database ..
            session.execute(
                FlowPRTModelInsert().values({
                    'creation_time': creation_time,
                    'expiration_time': expiration_time,
                    'reset_key_exp_time': reset_key_exp_time,
                    'user_id': user.user_id,
                    'token': prt,
                    'type_': const.password_reset.token_type,
                    'reset_key': reset_key,
                    'creation_ctx': json_dumps({
                        'remote_addr': [elem.exploded for elem in sso_ctx.remote_addr],
                        'user_agent': sso_ctx.user_agent,
                        'has_remote_addr': sso_ctx.has_remote_addr,
                        'has_user_agent': sso_ctx.has_user_agent,
                    }),
                    GENERIC.ATTR_NAME: json_dumps(None)
            }))

            # .. commit the operation.
            session.commit()

        # Now, we canot notify the user (note that we are doing it outside the "with" block above
        # so as not to block the SQL connection).
        self.send_notification(user, prt)

# ################################################################################################################################

    def access_token(self, cid, token, current_app, remote_addr, user_agent, _utcnow=datetime.utcnow, _timedelta=timedelta):
        # type: (str, str, str, str, str, object, object) -> str

        # For later use
        now = _utcnow()
        sso_ctx = self._build_sso_ctx(cid, remote_addr, user_agent, current_app)

        # We need an SQL session ..
        with closing(self.odb_session_func()) as session:

            # .. try to look up the user by the incoming PRT which must exist and not to have expired,
            # or otherwise have been invalidated (e.g. already accessed) ..
            user_info = get_user_by_prt(session, token, now)

            # .. no data matching the PRT, we need to reject the request ..
            if not user_info:
                msg = 'Token rejected. No valid PRT matched input `%s` (now: %s)'
                logger.warn(msg, token, now)
                raise ValidationError(status_code.password_reset.could_not_access, False)

            # .. now, check if the user is still allowed to access the system;
            # we make an assuption that it is true (the user is still allowed),
            # which is why we conduct this check under the same SQL session ..
            self.user_checker.check(sso_ctx, user_info)

            # .. if we are here, it means that the user checks above succeeded,
            # which means that we can modify the state to indicate that the token
            # has been accessed and we can return an encrypted access token
            # to the caller to let the user update the password ..

            session.execute(FlowPRTModelUpdate().where(
                FlowPRTModelTable.c.token==token
            ).values({
                'has_been_accessed': True,
                'access_time': now,
                'access_ctx': json_dumps({
                    'remote_addr': [elem.exploded for elem in sso_ctx.remote_addr],
                    'user_agent': user_agent,
                    'has_remote_addr': sso_ctx.has_remote_addr,
                    'has_user_agent': sso_ctx.has_user_agent,
                })
            }))

            # .. commit the operation.
            session.commit()

        # Now, outside the SQL block, encrypt the reset key and return it to the caller
        # so that the user can provide it in the subsequent call to reset the password.
        return self.server.encrypt(user_info.reset_key, prefix='')

# ################################################################################################################################

    def change_password(self, cid, new_password, token, reset_key, current_app, remote_addr, user_agent,
        _utcnow=datetime.utcnow, _timedelta=timedelta):
        # type: (str, str, str, str, str, str, object, object) -> str

        # For later use
        now = _utcnow()
        sso_ctx = self._build_sso_ctx(cid, remote_addr, user_agent, current_app)

        # This will be encrypted on input so we need to get a clear-text version of it
        reset_key = self.server.decrypt_no_prefix(reset_key)

        # We need an SQL session ..
        with closing(self.odb_session_func()) as session:

            # .. try to look up the user by the incoming PRT and reset key which must exist and not to have expired,
            # or otherwise have been invalidated (e.g. already accessed) ..
            user_info = get_user_by_prt_and_reset_key(session, token, reset_key, now)

            # .. no data matching the PRT, we need to reject the request ..
            if not user_info:
                msg = 'Token or reset key rejected. No valid PRT or reset key matched input `%s` `%s` (now: %s)'
                logger.warn(msg, token, reset_key, now)
                raise ValidationError(status_code.password_reset.could_not_access, False)

            # .. now, check if the user is still allowed to access the system;
            # we make an assuption that it is true (the user is still allowed),
            # which is why we conduct this check under the same SQL session ..
            self.user_checker.check(sso_ctx, user_info)

            # .. if we are here, it means that the user checks above succeeded
            # and we can update the user's password too ..
            self.server.sso_api.user.set_password(
                cid,
                user_info.user_id,
                new_password,
                must_change=False,
                password_expiry=None,
                current_app=current_app,
                remote_addr=remote_addr,
                details={
                    'user_agent': user_agent,
                    'has_remote_addr': sso_ctx.has_remote_addr,
                    'has_user_agent': sso_ctx.has_user_agent,
            })

            #
            # .. the password above was accepted and set which means that we can
            # modify the state to indicate that the reset key has been accessed
            # and that the password is changed ..
            #
            session.execute(FlowPRTModelUpdate().where(and_(
                FlowPRTModelTable.c.token==token,
                FlowPRTModelTable.c.reset_key==reset_key,
            )).values({
                'is_password_reset': True,
                'password_reset_time': now,
                'password_reset_ctx': json_dumps({
                    'remote_addr': [elem.exploded for elem in sso_ctx.remote_addr],
                    'user_agent': sso_ctx.user_agent,
                    'has_remote_addr': sso_ctx.has_remote_addr,
                    'has_user_agent': sso_ctx.has_user_agent,
                })
            }))

            # .. commit the operation.
            session.commit()

        # This is everything, we have just updated the user's password.

# ################################################################################################################################

    def send_notification(self, user, token, _template_name=CommonSSO.EmailTemplate.PasswordResetLink):
        # type: (SSOUser, str)

        if not self.smtp_conn_name:
            msg = 'Could not notify user `%s`, SSO SMTP connection not configured in sso.conf (main.smtp_conn)'
            logger.warn(msg, user.user_id)
            return

        # Decrypt email for later user
        user_email = self.decrypt_func(user.email)

        # Make sure an email is associated with the user
        if not user_email:
            logger.warn('Could not notify user `%s` (no email found)', user.user_id)
            return

        # When user preferences, including the preferred language, are added,
        # we can look it up here.
        pref_lang = Default.prt_locale

        # All email templates for the preferred language
        pref_lang_templates = self.server.static_config.sso.email.get(pref_lang) # type: Bunch

        # Make sure we have the correct templates prepared
        if not pref_lang_templates:
            msg = 'Could not send a password reset notification to `%s`. Language `%s` not found among `%s``'
            logger.warn(msg, user.user_id, pref_lang, sorted(self.server.static_config.sso.email))
            return

        # Template with the body to send
        template = pref_lang_templates.get(_template_name)

        # Make sure we have the correct templates prepared
        if not template:
            msg = 'Could not send a password reset notification to `%s`. Template `%s` not found among `%s`.'
            logger.warn(msg, user.user_id, _template_name, sorted(pref_lang_templates))
            return

        # Prepare the details for the template ..
        template_params = {
            'username': user.username,
            'site_name': self.site_name,
            'token': token,
            'expiration_time_hours': self.expiration_time_hours
        }

        # .. fill it in ..
        msg_body = template.format(**template_params)

        # .. get a handle to an SMTP connection ..
        smtp_conn = self.server.worker_store.email_smtp_api.get(self.smtp_conn_name).conn # type: SMTPConnection

        # .. create a new message ..
        msg = SMTPMessage()
        msg.is_html = False

        # .. provide metadata ..
        msg.subject = self.sso_conf.password_reset.get('email_title_' + pref_lang) or 'Password reset'
        msg.to = user_email
        msg.from_ = self.email_from

        # .. attach payload ..
        msg.body = msg_body

        # .. and send it to the user.
        smtp_conn.send(msg)

# ################################################################################################################################

    def _build_sso_ctx(self, cid, remote_addr, user_agent, current_app):
        # type: (str, str, str, str) -> None
        return SSOCtx(
            remote_addr=remote_addr,
            user_agent=user_agent,
            input=Bunch({
                'current_app': current_app,
            }),
            sso_conf=self.sso_conf,
            cid=cid,
        )

# ################################################################################################################################
# ################################################################################################################################
