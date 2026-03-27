;;; blackdog-test.el --- Tests and fixtures for Blackdog Emacs -*- lexical-binding: t; -*-

;;; Commentary:

;; Batchable ERT coverage for the Blackdog Emacs foundation helpers plus
;; fixture readers harvested from live Blackdog artifacts.

;;; Code:

(require 'cl-lib)
(require 'json)
(require 'ert)
(require 'blackdog-core)
(require 'blackdog-magit)

(defconst blackdog-test-root
  (expand-file-name
   (directory-file-name
    (locate-dominating-file
     (or load-file-name default-directory)
     "blackdog.toml"))))

(defconst blackdog-test-fixture-root
  (expand-file-name "fixtures/" (file-name-directory (or load-file-name (buffer-file-name))))
  "Directory containing frozen Blackdog fixtures.")

(defconst blackdog-test-task-id "BLACK-5036e0095a"
  "Task ID for the Emacs fixture set.")

(defconst blackdog-test-run-id "20260327-140022-8df46a17"
  "Supervisor run directory name for the Emacs fixture set.")

(defun blackdog-test--read-json (path)
  "Read PATH as JSON and return an alist with string keys."
  (with-temp-buffer
    (insert-file-contents path)
    (goto-char (point-min))
    (let ((json-object-type 'alist)
          (json-array-type 'vector)
          (json-key-type 'string)
          (json-false :json-false))
      (json-read))))

(defun blackdog-test-load-snapshot ()
  "Load the frozen snapshot fixture."
  (blackdog-test--read-json
   (expand-file-name "blackdog-snapshot.json" blackdog-test-fixture-root)))

(defun blackdog-test-list-task-result-files (task-id)
  "Return sorted result fixture files for TASK-ID."
  (let ((task-dir (expand-file-name (concat "task-results/" task-id)
                                    blackdog-test-fixture-root)))
    (when (file-directory-p task-dir)
      (sort (directory-files task-dir t "\\.json$" t) #'string<))))

(defun blackdog-test-load-task-result (task-id)
  "Load the first frozen result fixture for TASK-ID."
  (let ((result-files (blackdog-test-list-task-result-files task-id)))
    (unless result-files
      (error "No fixture result file for task %s" task-id))
    (blackdog-test--read-json (car result-files))))

(defun blackdog-test-load-supervisor-run-status (run-id)
  "Load the frozen supervisor status for RUN-ID."
  (blackdog-test--read-json
   (expand-file-name "status.json"
                     (expand-file-name run-id
                                       (expand-file-name "supervisor-runs"
                                                         blackdog-test-fixture-root)))))

(defun blackdog-test-run-task-file (run-id task-id name)
  "Return path to task artifact NAME for TASK-ID inside RUN-ID."
  (expand-file-name name
                    (expand-file-name task-id
                                      (expand-file-name run-id
                                                        (expand-file-name "supervisor-runs"
                                                                          blackdog-test-fixture-root)))))

(ert-deftest blackdog-project-root-finds-the-repo ()
  (let ((default-directory (expand-file-name "editors/emacs/test/" blackdog-test-root)))
    (should (equal blackdog-test-root (blackdog-project-root)))))

(ert-deftest blackdog-resolve-href-expands-control-dir-paths ()
  (let ((snapshot '((control_dir . "/tmp/blackdog"))))
    (should (equal "/tmp/blackdog/task-results/foo.json"
                   (blackdog-resolve-href "task-results/foo.json" snapshot)))
    (should (equal "https://example.com"
                   (blackdog-resolve-href "https://example.com" snapshot)))))

(ert-deftest blackdog-task-candidates-include-id-and-title ()
  (let* ((snapshot '((tasks . (((id . "TASK-1")
                                (title . "First task")
                                (operator_status . "Ready"))))))
         (candidates (blackdog-task-candidates snapshot)))
    (should (equal 1 (length candidates)))
    (should (string-match-p "TASK-1" (caar candidates)))
    (should (string-match-p "First task" (caar candidates)))))

(ert-deftest blackdog-magit-parses-worktree-porcelain ()
  (let* ((porcelain (mapconcat
                     #'identity
                     '("worktree /tmp/one"
                       "HEAD 1234"
                       "branch refs/heads/main"
                       "worktree /tmp/two"
                       "HEAD 5678"
                       "branch refs/heads/agent/task")
                     "\n"))
         (pairs (blackdog-magit--parse-worktrees porcelain)))
    (should (equal "/tmp/one" (alist-get "main" pairs nil nil #'string=)))
    (should (equal "/tmp/two" (alist-get "agent/task" pairs nil nil #'string=)))))

(ert-deftest blackdog-snapshot-live-loads-the-project ()
  (let ((snapshot (blackdog-snapshot blackdog-test-root t)))
    (should (equal "Blackdog" (alist-get 'project_name snapshot)))
    (should (alist-get 'tasks snapshot))
    (should (alist-get 'control_dir snapshot))))

(ert-deftest blackdog-test-snapshot-fixture-shape ()
  (skip-unless (file-exists-p (expand-file-name "blackdog-snapshot.json"
                                               blackdog-test-fixture-root)))
  (let ((snapshot (blackdog-test-load-snapshot)))
    (should (stringp (cdr (assoc "project_name" snapshot))))
    (should (stringp (cdr (assoc "project_root" snapshot))))
    (should (numberp (cdr (assoc "schema_version" snapshot))))
    (should (sequencep (cdr (assoc "tasks" snapshot))))
    (should (sequencep (cdr (assoc "recent_results" snapshot))))))

(ert-deftest blackdog-test-task-result-fixture-shape ()
  (skip-unless (blackdog-test-list-task-result-files blackdog-test-task-id))
  (let ((result (blackdog-test-load-task-result blackdog-test-task-id)))
    (should (string= (cdr (assoc "task_id" result)) blackdog-test-task-id))
    (should (string= (cdr (assoc "status" result)) "success"))
    (should (sequencep (cdr (assoc "what_changed" result))))
    (should (sequencep (cdr (assoc "validation" result))))))

(ert-deftest blackdog-test-supervisor-run-fixture-shape ()
  (skip-unless (file-exists-p
                (expand-file-name "status.json"
                                  (expand-file-name blackdog-test-run-id
                                                    (expand-file-name "supervisor-runs"
                                                                      blackdog-test-fixture-root)))))
  (let* ((run-status (blackdog-test-load-supervisor-run-status blackdog-test-run-id))
         (run-id (cdr (assoc "run_id" run-status)))
         (run-dir-base (file-name-nondirectory
                        (directory-file-name
                         (expand-file-name blackdog-test-run-id
                                           (expand-file-name "supervisor-runs"
                                                             blackdog-test-fixture-root)))))
         (run-prefix-matches (string-match-p
                              "^[0-9]+-[0-9]+-[0-9a-f]\\{8\\}$"
                              blackdog-test-run-id)))
    (should (stringp run-id))
    (should (string-match-p "^[0-9a-f]\\{8\\}$" run-id))
    (should (string-match-p run-id run-dir-base))
    (should run-prefix-matches)
    (should (string= (cdr (assoc "actor" run-status)) "supervisor/emacs"))
    (should (string= (cdr (assoc "workspace_mode" run-status)) "git-worktree"))
    (should (file-exists-p
             (blackdog-test-run-task-file blackdog-test-run-id blackdog-test-task-id "metadata.json")))
    (should (file-exists-p
             (blackdog-test-run-task-file blackdog-test-run-id blackdog-test-task-id "stdout.log")))
    (should (file-exists-p
             (blackdog-test-run-task-file blackdog-test-run-id blackdog-test-task-id "stderr.log")))
    (should (file-exists-p
             (blackdog-test-run-task-file blackdog-test-run-id blackdog-test-task-id "prompt.txt")))))

(provide 'blackdog-test)

;;; blackdog-test.el ends here
