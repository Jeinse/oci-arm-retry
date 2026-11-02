"""GitHub Contents API campaign journal, using only the built-in GITHUB_TOKEN."""
import base64
import copy
import json
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from oci_retry_micro import GuardError, initial_state, require

BRANCH = "e2-retry-state"
PATH = "state/e2-micro.json"


class GitHubState:
    def __init__(self, repository, token, source_sha, run_url):
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is not None, "INVALID_REPOSITORY")
        require(re.fullmatch(r"[a-f0-9]{40}", source_sha) is not None, "INVALID_SOURCE_SHA")
        require(bool(token), "GITHUB_TOKEN_REQUIRED")
        self.repository, self.token, self.source_sha = repository, token, source_sha
        self.run_url, self.sha, self.branch_exists = run_url, None, False

    @classmethod
    def from_env(cls):
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        require(run_id.isdigit(), "INVALID_RUN_ID")
        return cls(repo, os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_SHA", ""),
                   "https://github.com/" + repo + "/actions/runs/" + run_id)

    def request(self, method, path, body=None):
        request = Request("https://api.github.com/repos/" + self.repository + path,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json", "User-Agent": "e2-micro-single-attempt"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def load(self):
        try:
            self.request("GET", "/git/ref/heads/" + BRANCH)
            self.branch_exists = True
        except HTTPError as error:
            if error.code == 404:
                return initial_state()
            raise
        try:
            data = self.request("GET", "/contents/" + PATH + "?ref=" + BRANCH)
        except HTTPError as error:
            if error.code == 404:
                # A present journal branch without its journal may indicate manual deletion.
                # Do not silently reset an already-started campaign.
                raise GuardError("STATE_BRANCH_EXISTS_BUT_JOURNAL_MISSING") from None
            raise
        self.sha = data["sha"]
        state = json.loads(base64.b64decode(data["content"]))
        require(state.get("repository") == self.repository, "STATE_REPOSITORY_MISMATCH")
        return state

    def save(self, state):
        state = copy.deepcopy(state)
        state["repository"] = self.repository
        if not self.branch_exists:
            self.request("POST", "/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": self.source_sha})
            self.branch_exists = True
        payload = {"message": "Record E2.Micro campaign state", "branch": BRANCH,
                   "content": base64.b64encode((json.dumps(state, indent=2) + "\n").encode()).decode()}
        if self.sha:
            payload["sha"] = self.sha
        result = self.request("PUT", "/contents/" + PATH, payload)
        self.sha = result["content"]["sha"]

    def notify(self):
        # The issue contains no OCI resource OCID, IP address or credentials.
        self.request("POST", "/issues", {
            "title": "E2.Micro creation accepted — automatic creation has stopped",
            "body": "Oracle accepted the target instance creation or the managed target was found. "
                    "Check provisioning in OCI Console. Further creation requests are permanently blocked "
                    "by the completed journal.\n\nWorkflow: " + self.run_url,
            "assignees": [self.repository.split("/")[0]]})
