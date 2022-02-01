# -*- coding: utf-8 -*-

"""
Copyright (C) 2021, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# ################################################################################################################################
# ################################################################################################################################

class ServiceConst:
    ServiceInvokerName = 'pub.zato.service.service-invoker'

# ################################################################################################################################
# ################################################################################################################################

class SECRETS:

    # These parameters will be automatically encrypted in SimpleIO input
    PARAMS = ('auth_data', 'auth_token', 'password', 'password1', 'password2', 'secret_key', 'token', 'secret')

    # Zato secret (Fernet)
    PREFIX = 'zato.secf.'
    PREFIX_BYTES = b'zato.secf.'

    # Encrypted data has this prefix
    EncryptedMarker = 'gAAA'

    # Zato secret (configuration)
    URL_PREFIX = 'zato+secret://'

# ################################################################################################################################
# ################################################################################################################################
