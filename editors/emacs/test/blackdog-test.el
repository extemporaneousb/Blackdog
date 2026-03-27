;;; blackdog-test.el --- Emacs fixtures for Blackdog parser tests -*- lexical-binding: t; -*-

(require 'cl-lib)
(require 'json)
(require 'ert)

(defconst blackdog-test-fixture-root
  (expand-file-name "fixtures/" (file-name-directory (or load-file-name (buffer-file-name))))
  "Directory containing frozen Blackdog fixtures.")

(defconst blackdog-test-task-id "BLACK-5036e0095a"
  "Task ID for the fixture set.")

(defconst blackdog-test-run-id "20260327-140022-8df46a17"
  "Supervisor run ID for the fixture set.")

(defun blackdog-test--read-json (path)
  "Read PATH as JSON and return an alist."
  (with-temp-buffer
    (insert-file-contents path)
    (goto-char (point-min))
    (let ((json-object-type 'alist)
          (json-array-type 'vector)
          (json-key-type 'string)
          (json-false :json-false))
      (json-read))))

(defun blackdog-test-load-snapshot ()
  "Load the frozen full snapshot fixture as JSON."
  (blackdog-test--read-json (expand-file-name "blackdog-snapshot.json" blackdog-test-fixture-root)))

(defun blackdog-test-list-task-result-files (task-id)
  "Return sorted list of JSON result files for TASK-ID from the fixture tree."
  (let ((task-dir (expand-file-name (concat "task-results/" task-id) blackdog-test-fixture-root)))
    (when (file-directory-p task-dir)
      (sort (directory-files task-dir t "\\.json$" t) #'string<))))

(defun blackdog-test-load-task-result (task-id)
  "Load the first known result fixture for TASK-ID."
  (let ((result-files (blackdog-test-list-task-result-files task-id)))
    (unless result-files
      (error "No fixture result file for task %s in %s" task-id blackdog-test-task-id))
    (blackdog-test--read-json (car result-files))))

(defun blackdog-test-load-supervisor-run-status (run-id)
  "Load the supervisor run status fixture for RUN-ID."
  (blackdog-test--read-json
   (expand-file-name "status.json" (expand-file-name run-id (expand-file-name "supervisor-runs" blackdog-test-fixture-root)))))

(defun blackdog-test-run-task-file (run-id task-id name)
  "Return path to run TASK-ID artifact file NAME in RUN-ID fixture directory."
  (expand-file-name name
                    (expand-file-name task-id
                                      (expand-file-name run-id
                                                        (expand-file-name "supervisor-runs" blackdog-test-fixture-root)))))

(ert-deftest blackdog-test-snapshot-fixture-shape ()
  (let ((snapshot (blackdog-test-load-snapshot)))
    (should (stringp (cdr (assoc "project_name" snapshot))))
    (should (stringp (cdr (assoc "project_root" snapshot))))
    (should (numberp (cdr (assoc "schema_version" snapshot))))
    (should (sequencep (cdr (assoc "tasks" snapshot))))
    (should (sequencep (cdr (assoc "recent_results" snapshot))))))

(ert-deftest blackdog-test-task-result-fixture-shape ()
  (let ((result (blackdog-test-load-task-result blackdog-test-task-id)))
    (should (string= (cdr (assoc "task_id" result)) blackdog-test-task-id))
    (should (string= (cdr (assoc "status" result)) "success"))
    (should (sequencep (cdr (assoc "what_changed" result))))
    (should (sequencep (cdr (assoc "validation" result))))))

(ert-deftest blackdog-test-supervisor-run-fixture-shape ()
  (let* ((run-status (blackdog-test-load-supervisor-run-status blackdog-test-run-id))
         (run-id (cdr (assoc "run_id" run-status)))
         (run-dir-base (file-name-nondirectory
                        (directory-file-name
                         (expand-file-name blackdog-test-run-id
                                          (expand-file-name "supervisor-runs" blackdog-test-fixture-root)))))
         (run-prefix-matches (string-match-p
                             "^[0-9]+-[0-9]+-[0-9a-f]\\{8\\}$"
                             blackdog-test-run-id)))
    (should (stringp run-id))
    (should (string-match-p "^[0-9a-f]\\{8\\}$" run-id))
    (should (string-match-p run-id run-dir-base))
    (should run-prefix-matches)
    (should (string= (cdr (assoc "actor" run-status)) "supervisor/emacs"))
    (should (string= (cdr (assoc "workspace_mode" run-status)) "git-worktree"))
    (should (file-exists-p (blackdog-test-run-task-file blackdog-test-run-id blackdog-test-task-id "metadata.json")))
    (should (file-exists-p (blackdog-test-run-task-file blackdog-test-run-id blackdog-test-task-id "stdout.log")))
    (should (file-exists-p (blackdog-test-run-task-file blackdog-test-run-id blackdog-test-task-id "stderr.log")))
    (should (file-exists-p (blackdog-test-run-task-file blackdog-test-run-id blackdog-test-task-id "prompt.txt")))))

(provide 'blackdog-test)
;;; blackdog-test.el ends here
