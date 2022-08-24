import json
import os
import shutil
import tempfile
from os.path import join

from unittest import TestCase
from unittest.mock import patch

import httpretty
from click.testing import CliRunner

from rsconnect.json_web_token import ENV_VAR_PRIVATE_KEY_PASSWORD, is_jwt_compatible_python_version

from .utils import (
    apply_common_args,
    optional_ca_data,
    optional_target,
    get_dir,
    get_manifest_path,
    get_api_path,
    require_api_key,
    require_connect,
    generate_test_ed25519_keypair,
    convert_ed25519_private_key_to_bytes,
    has_jwt_structure,
)
from rsconnect.main import cli
from rsconnect import VERSION


def _error_to_response(error):
    """
    HTTPretty is unable to show errors resulting from callbacks, so this method attempts to raise failure visibility by
    passing the return back through HTTP.
    """
    return [500, {}, str(error)]


def _load_json(data):
    if isinstance(data, bytes):
        return json.loads(data.decode())
    return json.loads(data)


class TestMain(TestCase):
    def setUp(self):
        shutil.rmtree("test-home", ignore_errors=True)
        os.environ["HOME"] = "test-home"

    def require_connect(self):
        connect_server = os.environ.get("CONNECT_SERVER", None)
        if connect_server is None:
            self.skipTest("Set CONNECT_SERVER to test this function.")
        return connect_server

    def require_api_key(self):
        connect_api_key = os.environ.get("CONNECT_API_KEY", None)
        if connect_api_key is None:
            self.skipTest("Set CONNECT_API_KEY to test this function.")
        return connect_api_key

    @staticmethod
    def optional_target(default):
        return os.environ.get("CONNECT_DEPLOY_TARGET", default)

    @staticmethod
    def optional_ca_data(default=None):
        # noinspection SpellCheckingInspection
        return os.environ.get("CONNECT_CADATA_FILE", default)

    # noinspection SpellCheckingInspection
    def create_deploy_args(self, deploy_command, target):
        connect_server = require_connect(self)
        api_key = require_api_key(self)
        cadata_file = optional_ca_data(None)
        args = ["deploy", deploy_command]
        apply_common_args(args, server=connect_server, key=api_key, cacert=cadata_file)
        args.append(target)
        return args

    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["version"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(VERSION, result.output)

    def test_ping(self):
        connect_server = self.require_connect()
        runner = CliRunner()
        result = runner.invoke(cli, ["details", "-s", connect_server])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("OK", result.output)

    def test_ping_api_key(self):
        connect_server = require_connect(self)
        api_key = require_api_key(self)
        runner = CliRunner()
        args = ["details"]
        apply_common_args(args, server=connect_server, key=api_key)
        result = runner.invoke(cli, args)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("OK", result.output)

    def test_deploy(self):
        target = optional_target(get_dir(join("pip1", "dummy.ipynb")))
        runner = CliRunner()
        args = self.create_deploy_args("notebook", target)
        result = runner.invoke(cli, args)
        self.assertEqual(result.exit_code, 0, result.output)

    # noinspection SpellCheckingInspection
    def test_deploy_manifest(self):
        target = optional_target(get_manifest_path("shinyapp"))
        runner = CliRunner()
        args = self.create_deploy_args("manifest", target)
        result = runner.invoke(cli, args)
        self.assertEqual(result.exit_code, 0, result.output)

    # noinspection SpellCheckingInspection
    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_deploy_manifest_shinyapps(self):
        original_api_key_value = os.environ.pop("CONNECT_API_KEY", None)
        original_server_value = os.environ.pop("CONNECT_SERVER", None)

        httpretty.register_uri(
            httpretty.GET,
            "https://api.shinyapps.io/v1/users/me",
            body=open("tests/testdata/shinyapps-responses/get-user.json", "r").read(),
            status=200,
        )
        httpretty.register_uri(
            httpretty.GET,
            "https://api.shinyapps.io/v1/applications"
            "?filter=name:like:shinyapp&offset=0&count=100&use_advanced_filters=true",
            body=open("tests/testdata/shinyapps-responses/get-applications.json", "r").read(),
            adding_headers={"Content-Type": "application/json"},
            status=200,
        )
        httpretty.register_uri(
            httpretty.GET,
            "https://api.shinyapps.io/v1/accounts/",
            body=open("tests/testdata/shinyapps-responses/get-accounts.json", "r").read(),
            adding_headers={"Content-Type": "application/json"},
            status=200,
        )

        def post_application_callback(request, uri, response_headers):
            parsed_request = _load_json(request.body)
            try:
                self.assertDictEqual(parsed_request, {"account": 82069, "name": "myapp", "template": "shiny"})
            except AssertionError as e:
                return _error_to_response(e)
            return [
                201,
                {"Content-Type": "application/json"},
                open("tests/testdata/shinyapps-responses/create-application.json", "r").read(),
            ]

        httpretty.register_uri(
            httpretty.POST,
            "https://api.shinyapps.io/v1/applications/",
            body=post_application_callback,
            status=200,
        )

        def post_bundle_callback(request, uri, response_headers):
            parsed_request = _load_json(request.body)
            del parsed_request["checksum"]
            del parsed_request["content_length"]
            try:
                self.assertDictEqual(
                    parsed_request,
                    {
                        "application": 8442,
                        "content_type": "application/x-tar",
                    },
                )
            except AssertionError as e:
                return _error_to_response(e)
            return [
                201,
                {"Content-Type": "application/json"},
                open("tests/testdata/shinyapps-responses/create-bundle.json", "r").read(),
            ]

        httpretty.register_uri(
            httpretty.POST,
            "https://api.shinyapps.io/v1/bundles",
            body=post_bundle_callback,
        )

        httpretty.register_uri(
            httpretty.PUT,
            "https://lucid-uploads-staging.s3.amazonaws.com/bundles/application-8442/"
            "6c9ed0d91ee9426687d9ac231d47dc83.tar.gz"
            "?AWSAccessKeyId=theAccessKeyId"
            "&Signature=dGhlU2lnbmF0dXJlCg%3D%3D"
            "&content-md5=D1blMI4qTiI3tgeUOYXwkg%3D%3D"
            "&content-type=application%2Fx-tar"
            "&x-amz-security-token=dGhlVG9rZW4K"
            "&Expires=1656715153",
            body="",
        )

        def post_bundle_status_callback(request, uri, response_headers):
            parsed_request = _load_json(request.body)
            try:
                self.assertDictEqual(parsed_request, {"status": "ready"})
            except AssertionError as e:
                return _error_to_response(e)
            return [303, {"Location": "https://api.shinyapps.io/v1/bundles/12640"}, ""]

        httpretty.register_uri(
            httpretty.POST,
            "https://api.shinyapps.io/v1/bundles/12640/status",
            body=post_bundle_status_callback,
        )

        httpretty.register_uri(
            httpretty.GET,
            "https://api.shinyapps.io/v1/bundles/12640",
            body=open("tests/testdata/shinyapps-responses/get-accounts.json", "r").read(),
            adding_headers={"Content-Type": "application/json"},
            status=200,
        )

        def post_deploy_callback(request, uri, response_headers):
            parsed_request = _load_json(request.body)
            try:
                self.assertDictEqual(parsed_request, {"bundle": 12640, "rebuild": False})
            except AssertionError as e:
                return _error_to_response(e)
            return [
                303,
                {"Location": "https://api.shinyapps.io/v1/tasks/333"},
                open("tests/testdata/shinyapps-responses/post-deploy.json", "r").read(),
            ]

        httpretty.register_uri(
            httpretty.POST,
            "https://api.shinyapps.io/v1/applications/8442/deploy",
            body=post_deploy_callback,
        )

        httpretty.register_uri(
            httpretty.GET,
            "https://api.shinyapps.io/v1/tasks/333",
            body=open("tests/testdata/shinyapps-responses/get-task.json", "r").read(),
            adding_headers={"Content-Type": "application/json"},
            status=200,
        )

        runner = CliRunner()
        args = [
            "deploy",
            "manifest",
            get_manifest_path("shinyapp"),
            "--account",
            "some-account",
            "--token",
            "someToken",
            "--secret",
            "c29tZVNlY3JldAo=",
            "--title",
            "myApp",
        ]
        try:
            result = runner.invoke(cli, args)
            self.assertEqual(result.exit_code, 0, result.output)
        finally:
            if original_api_key_value:
                os.environ["CONNECT_API_KEY"] = original_api_key_value
            if original_server_value:
                os.environ["CONNECT_SERVER"] = original_server_value

    def test_deploy_api(self):
        target = optional_target(get_api_path("flask"))
        runner = CliRunner()
        args = self.create_deploy_args("api", target)
        result = runner.invoke(cli, args)
        self.assertEqual(result.exit_code, 0, result.output)

    def test_add_connect(self):
        connect_server = self.require_connect()
        api_key = self.require_api_key()
        runner = CliRunner()
        result = runner.invoke(cli, ["add", "--name", "my-connect", "--server", connect_server, "--api-key", api_key])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("OK", result.output)

    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_add_shinyapps(self):
        original_api_key_value = os.environ.pop("CONNECT_API_KEY", None)
        original_server_value = os.environ.pop("CONNECT_SERVER", None)
        try:
            httpretty.register_uri(
                httpretty.GET, "https://api.shinyapps.io/v1/users/me", body='{"id": 1000}', status=200
            )

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "add",
                    "--account",
                    "some-account",
                    "--name",
                    "my-shinyapps",
                    "--token",
                    "someToken",
                    "--secret",
                    "c29tZVNlY3JldAo=",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("shinyapps.io credential", result.output)

        finally:
            if original_api_key_value:
                os.environ["CONNECT_API_KEY"] = original_api_key_value
            if original_server_value:
                os.environ["CONNECT_SERVER"] = original_server_value

    def test_add_shinyapps_missing_options(self):
        original_api_key_value = os.environ.pop("CONNECT_API_KEY", None)
        original_server_value = os.environ.pop("CONNECT_SERVER", None)
        try:
            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "add",
                    "--name",
                    "my-shinyapps",
                    "--token",
                    "someToken",
                ],
            )
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertEqual(
                str(result.exception),
                "-A/--account, -T/--token, and -S/--secret must all be provided for shinyapps.io.",
            )
        finally:
            if original_api_key_value:
                os.environ["CONNECT_API_KEY"] = original_api_key_value
            if original_server_value:
                os.environ["CONNECT_SERVER"] = original_server_value


class TestInitialAdmin(TestCase):
    def setUp(self):
        if not is_jwt_compatible_python_version():
            self.skipTest("JWTs not supported in Python < 3.6")

    def create_initial_admin_mock_callback(self, status, json_data):
        def request_callback(request, uri, response_headers):

            # verify auth header is sent correctly
            authorization = request.headers.get("Authorization")
            auth_split = authorization.split(" ")
            self.assertEqual(len(auth_split), 2)
            self.assertEqual(auth_split[0], "Bearer")
            self.assertTrue(has_jwt_structure(auth_split[1]))

            # verify uri
            self.assertEqual(uri, "http://localhost:8080/__api__/v1/experimental/installation/initial_admin")

            return [status, {"Content-Type": "application/json"}, json.dumps(json_data)]

        return request_callback

    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_initial_admin(self):
        """
        Normal initial-admin operation
        """

        callback = self.create_initial_admin_mock_callback(200, {"api_key": "testapikey123"})

        httpretty.register_uri(
            httpretty.POST,
            "http://localhost:8080/__api__/v1/experimental/installation/initial_admin",
            body=callback,
        )

        private_key, _ = generate_test_ed25519_keypair()
        private_key_bytes = convert_ed25519_private_key_to_bytes(private_key)

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()
            with tempfile.TemporaryDirectory() as td:
                # create a temporaray private keyfile
                private_keyfile = os.path.join(td, "test_ed25519")
                with open(private_keyfile, "wb") as f:
                    f.write(private_key_bytes)

                result = runner.invoke(
                    cli,
                    [
                        "initial-admin",
                        "--server",
                        "http://localhost:8080",
                        "--jwt-keypath",
                        private_keyfile,
                        "--insecure",
                    ],
                )

                self.assertEqual(result.exit_code, 0, result.output)

                json_output = json.loads(result.output)
                expected_output = json.loads(open("tests/testdata/initial-admin-responses/success.json", "r").read())
                self.assertEqual(json_output, expected_output)

        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_initial_admin_misc_error(self):
        """
        Fail reasonable if response indicates that request is unauthorized
        """
        callback = self.create_initial_admin_mock_callback(500, {})

        httpretty.register_uri(
            httpretty.POST,
            "http://localhost:8080/__api__/v1/experimental/installation/initial_admin",
            body=callback,
        )

        private_key, _ = generate_test_ed25519_keypair()
        private_key_bytes = convert_ed25519_private_key_to_bytes(private_key)

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()
            with tempfile.TemporaryDirectory() as td:
                # create a temporaray private keyfile
                private_keyfile = os.path.join(td, "test_ed25519")
                with open(private_keyfile, "wb") as f:
                    f.write(private_key_bytes)

                result = runner.invoke(
                    cli,
                    [
                        "initial-admin",
                        "--server",
                        "http://localhost:8080",
                        "--jwt-keypath",
                        private_keyfile,
                        "--insecure",
                    ],
                )
                self.assertEqual(result.exit_code, 0, result.output)

                json_output = json.loads(result.output)
                expected_output = json.loads(
                    open("tests/testdata/initial-admin-responses/other_error.json", "r").read()
                )
                self.assertEqual(json_output, expected_output)
        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_initial_admin_not_found_error(self):
        """
        Fail reasonable if response indicates that request is unauthorized
        """

        callback = self.create_initial_admin_mock_callback(404, {})

        httpretty.register_uri(
            httpretty.POST,
            "http://localhost:8080/__api__/v1/experimental/installation/initial_admin",
            body=callback,
        )

        private_key, _ = generate_test_ed25519_keypair()
        private_key_bytes = convert_ed25519_private_key_to_bytes(private_key)

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()
            with tempfile.TemporaryDirectory() as td:
                # create a temporaray private keyfile
                private_keyfile = os.path.join(td, "test_ed25519")
                with open(private_keyfile, "wb") as f:
                    f.write(private_key_bytes)

                result = runner.invoke(
                    cli,
                    [
                        "initial-admin",
                        "--server",
                        "http://localhost:8080",
                        "--jwt-keypath",
                        private_keyfile,
                        "--insecure",
                    ],
                )
                self.assertEqual(result.exit_code, 0, result.output)

                json_output = json.loads(result.output)
                expected_output = json.loads(
                    open("tests/testdata/initial-admin-responses/not_found_error.json", "r").read()
                )
                self.assertEqual(json_output, expected_output)
        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_initial_admin_client_error(self):
        """
        Fail reasonable if response indicates that request is unauthorized
        """

        callback = self.create_initial_admin_mock_callback(400, {})

        httpretty.register_uri(
            httpretty.POST,
            "http://localhost:8080/__api__/v1/experimental/installation/initial_admin",
            body=callback,
        )

        private_key, _ = generate_test_ed25519_keypair()
        private_key_bytes = convert_ed25519_private_key_to_bytes(private_key)

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()
            with tempfile.TemporaryDirectory() as td:
                # create a temporaray private keyfile
                private_keyfile = os.path.join(td, "test_ed25519")
                with open(private_keyfile, "wb") as f:
                    f.write(private_key_bytes)

                result = runner.invoke(
                    cli,
                    [
                        "initial-admin",
                        "--server",
                        "http://localhost:8080",
                        "--jwt-keypath",
                        private_keyfile,
                        "--insecure",
                    ],
                )
                self.assertEqual(result.exit_code, 0, result.output)

                json_output = json.loads(result.output)
                expected_output = json.loads(
                    open("tests/testdata/initial-admin-responses/client_error.json", "r").read()
                )

                self.assertEqual(json_output, expected_output)
        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_initial_admin_unauthorized(self):
        """
        Fail reasonable if response indicates that request is unauthorized
        """

        callback = self.create_initial_admin_mock_callback(401, {})

        httpretty.register_uri(
            httpretty.POST,
            "http://localhost:8080/__api__/v1/experimental/installation/initial_admin",
            body=callback,
        )

        private_key, _ = generate_test_ed25519_keypair()
        private_key_bytes = convert_ed25519_private_key_to_bytes(private_key)

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()
            with tempfile.TemporaryDirectory() as td:
                # create a temporaray private keyfile
                private_keyfile = os.path.join(td, "test_ed25519")
                with open(private_keyfile, "wb") as f:
                    f.write(private_key_bytes)

                result = runner.invoke(
                    cli,
                    [
                        "initial-admin",
                        "--server",
                        "http://localhost:8080",
                        "--jwt-keypath",
                        private_keyfile,
                        "--insecure",
                    ],
                )
                self.assertEqual(result.exit_code, 0, result.output)

                json_output = json.loads(result.output)
                expected_output = json.loads(
                    open("tests/testdata/initial-admin-responses/unauthorized_error.json", "r").read()
                )

                self.assertEqual(json_output, expected_output)
        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    def test_initial_admin_help(self):
        """
        Help parameter should complete without erroring
        """

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()
            result = runner.invoke(cli, ["initial-admin", "--help"])
            self.assertEqual(result.exit_code, 0, result.output)
        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    def test_initial_admin_invalid_jwt_path(self):
        """
        Fail reasonably if jwt does not exist at provided path
        """

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()
            result = runner.invoke(
                cli, ["initial-admin", "--server", "http://host:port", "--jwt-keypath", "this/is/invalid"]
            )
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertEqual(result.output, "Error: Keypath does not exist.\n")
        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    def test_initial_admin_missing_options(self):
        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()

            # missing server
            result = runner.invoke(cli, ["initial-admin"])
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertEqual(result.output, "Error: You must specify -s/--server.\n")

            # missing jwt keypath
            result = runner.invoke(cli, ["initial-admin", "--server", "a_server"])
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertEqual(result.output, "Error: You must specify -j/--jwt-keypath.\n")

        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_initial_admin_raw_output(self):
        """
        Verify we can get the API key as raw output
        """

        expected_api_key = "apikey123"
        callback = self.create_initial_admin_mock_callback(200, {"api_key": expected_api_key})

        httpretty.register_uri(
            httpretty.POST,
            "http://localhost:8080/__api__/v1/experimental/installation/initial_admin",
            body=callback,
        )

        private_key, _ = generate_test_ed25519_keypair()
        private_key_bytes = convert_ed25519_private_key_to_bytes(private_key)

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)
        try:
            runner = CliRunner()
            with tempfile.TemporaryDirectory() as td:
                # create a temporaray private keyfile
                private_keyfile = os.path.join(td, "test_ed25519")
                with open(private_keyfile, "wb") as f:
                    f.write(private_key_bytes)

                result = runner.invoke(
                    cli,
                    [
                        "initial-admin",
                        "--server",
                        "http://localhost:8080",
                        "--jwt-keypath",
                        private_keyfile,
                        "--insecure",
                        "--raw",
                    ],
                )
                self.assertEqual(result.exit_code, 0, result.output)

                self.assertEqual(result.output, expected_api_key)
        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password

    @httpretty.activate(verbose=True, allow_net_connect=False)
    def test_initial_admin_password(self):

        secret_key_password = "a_password123!"
        expected_api_key = "apikey123"
        callback = self.create_initial_admin_mock_callback(200, {"api_key": expected_api_key})

        httpretty.register_uri(
            httpretty.POST,
            "http://localhost:8080/__api__/v1/experimental/installation/initial_admin",
            body=callback,
        )

        private_key, _ = generate_test_ed25519_keypair()
        private_key_bytes = convert_ed25519_private_key_to_bytes(private_key, password=secret_key_password)

        original_env_var_private_key_password = os.environ.pop(ENV_VAR_PRIVATE_KEY_PASSWORD, None)

        try:
            runner = CliRunner()
            with tempfile.TemporaryDirectory() as td:
                private_keyfile = os.path.join(td, "test_ed25519")
                with open(private_keyfile, "wb") as f:
                    f.write(private_key_bytes)

                # run with no env variable set, verify the failure
                result = runner.invoke(
                    cli,
                    [
                        "initial-admin",
                        "--server",
                        "http://localhost:8080",
                        "--jwt-keypath",
                        private_keyfile,
                        "--insecure",
                        "--raw",
                    ],
                )
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertEqual(result.output, "Error: Unable to load private key - it may be password-protected.\n")

                # pretend we're getting the password from the CLI
                with patch("rsconnect.json_web_token._load_private_key_password_interactive") as fn_interactive:
                    fn_interactive.return_value = secret_key_password
                    # run with no env variable set but correctly get the password from the command line
                    result = runner.invoke(
                        cli,
                        [
                            "initial-admin",
                            "--server",
                            "http://localhost:8080",
                            "--jwt-keypath",
                            private_keyfile,
                            "--insecure",
                            "--raw",
                            "--jwt-key-password",
                        ],
                    )
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(result.output, expected_api_key)

                with patch("rsconnect.json_web_token._load_private_key_password_interactive") as fn_interactive:
                    fn_interactive.return_value = "incorrect_password"
                    # run with no env variable set but incorrectly get the password from the command line
                    result = runner.invoke(
                        cli,
                        [
                            "initial-admin",
                            "--server",
                            "http://localhost:8080",
                            "--jwt-keypath",
                            private_keyfile,
                            "--insecure",
                            "--raw",
                            "--jwt-key-password",
                        ],
                    )
                    self.assertEqual(result.exit_code, 1, result.output)
                    self.assertEqual(
                        result.output, "Error: Unable to load private key - it may be password-protected.\n"
                    )

                # set the env variable
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = secret_key_password

                # run with an env variable
                result = runner.invoke(
                    cli,
                    [
                        "initial-admin",
                        "--server",
                        "http://localhost:8080",
                        "--jwt-keypath",
                        private_keyfile,
                        "--insecure",
                        "--raw",
                    ],
                )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(result.output, expected_api_key)

                # confirm that we ignore the CLI result if we have an env variable
                with patch("rsconnect.json_web_token._load_private_key_password_interactive") as fn_interactive:
                    # result coming from the command line should not matter
                    fn_interactive.return_value = "incorrect_password"
                    result = runner.invoke(
                        cli,
                        [
                            "initial-admin",
                            "--server",
                            "http://localhost:8080",
                            "--jwt-keypath",
                            private_keyfile,
                            "--insecure",
                            "--raw",
                            "--jwt-key-password",
                        ],
                    )
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(result.output, expected_api_key)

        finally:
            if original_env_var_private_key_password:
                os.environ[ENV_VAR_PRIVATE_KEY_PASSWORD] = original_env_var_private_key_password
