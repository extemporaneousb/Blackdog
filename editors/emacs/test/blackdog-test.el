;;; blackdog-test.el --- Tests and fixtures for Blackdog Emacs -*- lexical-binding: t; -*-

;;; Commentary:

;; Batchable ERT coverage for the Blackdog Emacs foundation helpers plus
;; fixture readers harvested from live Blackdog artifacts.

;;; Code:

(require 'cl-lib)
(require 'json)
(require 'ert)

(defconst blackdog-test-lisp-dir
  (expand-file-name
   "../lisp/"
   (file-name-directory (or load-file-name (buffer-file-name)))
   )
  "Directory containing the Emacs package source under editors/emacs.")

(when (file-directory-p blackdog-test-lisp-dir)
  (add-to-list 'load-path blackdog-test-lisp-dir))

(require 'blackdog-core)
(require 'blackdog-magit)
(require 'blackdog-task)

(defconst blackdog-test-has-magit-section
  (locate-library "magit-section")
  "Non-nil when magit-section can be loaded in this test environment.")

(when blackdog-test-has-magit-section
  (require 'blackdog-dashboard))

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

(defconst blackdog-test-fixture-candidate-clis
  (list (expand-file-name ".VE/bin/blackdog" blackdog-test-root)
        (executable-find "blackdog"))
  "Candidate Blackdog CLI commands for test execution.")

(defun blackdog-test--cli-command ()
  "Return a best-effort Blackdog command for integration-style tests."
  (or (let ((env-value (getenv "BLACKDOG_TEST_BLACKDOG_COMMAND")))
        (and (stringp env-value) (file-executable-p env-value) env-value))
      (seq-find #'file-executable-p
                (seq-filter #'identity blackdog-test-fixture-candidate-clis))))

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
  (let ((command (blackdog-test--cli-command)))
    (skip-unless command)
    (let ((blackdog-default-command command)
          (snapshot (blackdog-snapshot blackdog-test-root t)))
      (should (equal "Blackdog" (alist-get 'project_name snapshot)))
      (should (alist-get 'tasks snapshot))
      (should (alist-get 'control_dir snapshot)))))

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

(ert-deftest blackdog-task-view-inserts-quick-links ()
  (let ((task nil)
        (buffer (generate-new-buffer " *Blackdog Task Test*")))
    (setq task (list (cons 'id blackdog-test-task-id)
                     (cons 'title "Snapshot task")
                     (cons 'links (list (list (cons 'label "Prompt")
                                              (cons 'href "supervisor-runs/prompt.txt"))))))
    (cl-letf (((symbol-function #'blackdog-snapshot)
               (lambda (&optional _root _force) (list (cons 'tasks nil))))
              ((symbol-function #'blackdog-task-by-id)
               (lambda (_task-id &optional _snapshot _root) task)))
      (with-current-buffer buffer
        (blackdog-task-view-mode)
        (setq-local blackdog-buffer-root blackdog-test-root)
        (setq-local blackdog-task-id blackdog-test-task-id)
        (setq-local blackdog-task-data task)
        (blackdog-task-view-refresh)
        (should (string-match-p "Artifact Links" (buffer-string)))
        (should (string-match-p "Prompt" (buffer-string)))
        (should-not (string-match-p "Diff" (buffer-string)))
        (kill-buffer buffer))))

(ert-deftest blackdog-task-view-open-prompt-and-result-commands ()
  (let ((opened nil)
        (task (list (cons 'id "BLACK-1234")
                    (cons 'prompt_href "supervisor-runs/prompt.txt")
                    (cons 'latest_result_href "task-results/BLACK-1234/result.json")
                    (cons 'diff_href "supervisor-runs/changes.diff"))))
    (cl-letf (((symbol-function #'blackdog-open-href)
               (lambda (href &optional _snapshot _root _other-window)
                 (setq opened href))))
      (blackdog-task-view-open-prompt task)
      (should (equal "supervisor-runs/prompt.txt" opened))
      (blackdog-task-view-open-result task)
      (should (equal "task-results/BLACK-1234/result.json" opened))
      (let ((magit-called nil))
          (cl-letf (((symbol-function #'blackdog-task-view-magit-diff)
                     (lambda (_task _root)
                       (setq magit-called t))))
            (blackdog-task-view-open-diff task)
            (should (equal "supervisor-runs/changes.diff" opened))
            (should (eq nil magit-called))))))

(ert-deftest blackdog-dashboard-renders-sections ()
  (skip-unless blackdog-test-has-magit-section)
  (skip-unless (file-exists-p
                (expand-file-name "blackdog-snapshot.json" blackdog-test-fixture-root)))
(let* ((snapshot (blackdog-test-load-snapshot))
         (buffer (generate-new-buffer " *Blackdog Dashboard Test*")))
    (cl-letf (((symbol-function #'blackdog-snapshot)
               (lambda (&optional _root _force) snapshot)))
      (with-current-buffer buffer
        (blackdog-dashboard-mode)
        (setq-local blackdog-buffer-root blackdog-test-root)
        (blackdog-dashboard-refresh)
        (should (string-match-p "Overview" (buffer-substring-no-properties (point-min) (point-max))))
        (should (string-match-p "Objectives" (buffer-substring-no-properties (point-min) (point-max))))
        (should (string-match-p "Board Tasks" (buffer-substring-no-properties (point-min) (point-max))))
        (should (string-match-p "Queue" (buffer-string))))
      (kill-buffer buffer))))
)
)

(provide 'blackdog-test)

;;; blackdog-test.el ends here
