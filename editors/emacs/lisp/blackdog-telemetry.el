;;; blackdog-telemetry.el --- Supervisor telemetry and live monitoring -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Read-only telemetry surface for session-local Emacs instrumentation,
;; supervisor status/report/recover summaries, and a live monitor for
;; branch-backed supervisor runs.

;;; Code:

(require 'blackdog-core)
(require 'button)
(require 'seq)
(require 'subr-x)

(declare-function blackdog-runs-open "blackdog-runs" (&optional root))

(defcustom blackdog-telemetry-supervisor-actor "supervisor/emacs"
  "Supervisor actor to inspect from Emacs."
  :type 'string
  :group 'blackdog)

(defcustom blackdog-telemetry-auto-refresh-interval 2.0
  "Refresh interval in seconds for live supervisor monitoring.

Set to nil or 0 to disable automatic refresh."
  :type '(choice (const :tag "Disabled" nil) number)
  :group 'blackdog)

(defcustom blackdog-telemetry-supervisor-poll-interval-seconds 2.0
  "Poll interval passed to `blackdog supervise run' from Emacs."
  :type 'number
  :group 'blackdog)

(defcustom blackdog-telemetry-log-tail-bytes 8192
  "Maximum trailing bytes to read from one supervisor log artifact."
  :type 'integer
  :group 'blackdog)

(defcustom blackdog-telemetry-log-tail-lines 40
  "Maximum trailing lines to render from one supervisor log artifact."
  :type 'integer
  :group 'blackdog)

(defvar-local blackdog-telemetry-actor nil
  "Supervisor actor for the current telemetry buffer.")

(defvar-local blackdog-telemetry-refresh-timer nil
  "Auto-refresh timer for the current telemetry buffer.")

(defvar-local blackdog-telemetry-supervisor-process nil
  "Local asynchronous `blackdog supervise run' process for this buffer.")

(defvar-local blackdog-telemetry-supervisor-output-buffer nil
  "Stdout buffer for the current asynchronous supervisor process.")

(defvar-local blackdog-telemetry-supervisor-error-buffer nil
  "Stderr buffer for the current asynchronous supervisor process.")

(defvar-local blackdog-telemetry-last-status nil
  "Most recent supervisor status payload rendered in this buffer.")

(defvar blackdog-telemetry-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "c") #'blackdog-telemetry-clear-session)
    (define-key map (kbd "S") #'blackdog-telemetry-start-supervisor)
    (define-key map (kbd "x") #'blackdog-telemetry-stop-supervisor)
    (define-key map (kbd "u") #'blackdog-telemetry-open-latest-run)
    (define-key map (kbd "o") #'blackdog-telemetry-open-child-artifacts)
    (define-key map (kbd "r") #'blackdog-telemetry-open-runs)
    map)
  "Keymap for `blackdog-telemetry-mode'.")

(define-derived-mode blackdog-telemetry-mode special-mode "Blackdog-Telemetry"
  "Read-only telemetry buffer for Blackdog.")

(defun blackdog-telemetry--actor ()
  "Return the current telemetry actor."
  (or blackdog-telemetry-actor
      blackdog-telemetry-supervisor-actor))

(defun blackdog-telemetry-supervisor-status (&optional root actor)
  "Return supervisor status JSON for ROOT and ACTOR."
  (blackdog--call-json
   (or root (blackdog-project-root))
   "supervise" "status"
   "--actor" (or actor (blackdog-telemetry--actor))
   "--format" "json"))

(defun blackdog-telemetry-supervisor-report (&optional root actor)
  "Return supervisor report JSON for ROOT and ACTOR."
  (blackdog--call-json
   (or root (blackdog-project-root))
   "supervise" "report"
   "--actor" (or actor (blackdog-telemetry--actor))
   "--format" "json"))

(defun blackdog-telemetry-supervisor-recover (&optional root actor)
  "Return supervisor recovery JSON for ROOT and ACTOR."
  (blackdog--call-json
   (or root (blackdog-project-root))
   "supervise" "recover"
   "--actor" (or actor (blackdog-telemetry--actor))
   "--format" "json"))

(defun blackdog-telemetry-open (&optional root actor)
  "Open the Blackdog telemetry buffer for ROOT and ACTOR."
  (interactive)
  (let ((buffer (get-buffer-create "*Blackdog Telemetry*")))
    (with-current-buffer buffer
      (blackdog-telemetry-mode)
      (setq-local blackdog-buffer-root (or root (blackdog-project-root)))
      (setq-local blackdog-telemetry-actor (or actor blackdog-telemetry-supervisor-actor))
      (setq-local blackdog-refresh-function #'blackdog-telemetry-refresh)
      (add-hook 'kill-buffer-hook #'blackdog-telemetry--cleanup nil t)
      (blackdog-telemetry-refresh))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-telemetry--cleanup ()
  "Stop local telemetry timers when the current buffer is killed."
  (when (timerp blackdog-telemetry-refresh-timer)
    (cancel-timer blackdog-telemetry-refresh-timer)
    (setq blackdog-telemetry-refresh-timer nil)))

(defun blackdog-telemetry-clear-session ()
  "Clear Emacs-side telemetry counters and refresh the buffer."
  (interactive)
  (blackdog-clear-telemetry)
  (blackdog-telemetry-refresh))

(defun blackdog-telemetry-start-supervisor (&optional root actor)
  "Start one asynchronous supervisor run for ROOT and ACTOR."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (actor (or actor
                    (if current-prefix-arg
                        (read-string "Supervisor actor: " (blackdog-telemetry--actor))
                      (blackdog-telemetry--actor))))
         (buffer (blackdog-telemetry-open root actor)))
    (with-current-buffer buffer
      (setq-local blackdog-telemetry-actor actor)
      (when (process-live-p blackdog-telemetry-supervisor-process)
        (user-error "Supervisor process is already running for %s" actor))
      (let* ((status (condition-case nil
                         (blackdog-telemetry-supervisor-status root actor)
                       (error nil)))
             (latest-run (and (listp status) (alist-get 'latest_run status)))
             (latest-status (or (alist-get 'status latest-run)
                                (alist-get 'final_status latest-run)
                                "")))
        (when (member latest-status '("running" "draining"))
          (user-error "Supervisor actor %s already has a live run" actor)))
      (let* ((stdout-buffer (get-buffer-create
                             (format " *Blackdog Supervisor Stdout: %s*" actor)))
             (stderr-buffer (get-buffer-create
                             (format " *Blackdog Supervisor Stderr: %s*" actor))))
        (with-current-buffer stdout-buffer
          (erase-buffer))
        (with-current-buffer stderr-buffer
          (erase-buffer))
        (let* ((process-name (format "blackdog-supervisor-%s" actor))
               (process
                (blackdog-start-process
                 process-name
                 root
                 stdout-buffer
                 stderr-buffer
                 "supervise" "run"
                 "--actor" actor
                 "--poll-interval-seconds"
                 (number-to-string blackdog-telemetry-supervisor-poll-interval-seconds))))
          (setq-local blackdog-telemetry-supervisor-output-buffer stdout-buffer)
          (setq-local blackdog-telemetry-supervisor-error-buffer stderr-buffer)
          (setq-local blackdog-telemetry-supervisor-process process)
          (set-process-sentinel
           process
           (lambda (_process _event)
             (when (buffer-live-p buffer)
               (with-current-buffer buffer
                 (blackdog-telemetry-refresh)))))
          (blackdog-telemetry--ensure-auto-refresh t)
          (blackdog-telemetry-refresh)
          (message "Started %s" actor))))))

(defun blackdog-telemetry-stop-supervisor (&optional root actor)
  "Request a draining stop for the supervisor run at ROOT for ACTOR."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (actor (or actor (blackdog-telemetry--actor)))
         (sender (or blackdog-default-agent "emacs")))
    (blackdog--call-json
     root
     "inbox" "send"
     "--sender" sender
     "--recipient" actor
     "--tag" "stop"
     "--body" "stop after the current running tasks finish")
    (blackdog-telemetry-refresh)
    (message "Requested supervisor stop for %s" actor)))

(defun blackdog-telemetry-open-runs (&optional root)
  "Open the supervisor runs listing for ROOT."
  (interactive)
  (require 'blackdog-runs)
  (blackdog-runs-open (or root blackdog-buffer-root (blackdog-project-root))))

(defun blackdog-telemetry-open-latest-run (&optional root actor)
  "Open the latest supervisor run directory for ROOT and ACTOR."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (status (blackdog-telemetry-supervisor-status root actor))
         (latest-run (alist-get 'latest_run status))
         (run-dir (alist-get 'run_dir latest-run)))
    (unless run-dir
      (user-error "No latest supervisor run is available"))
    (blackdog-open-href run-dir nil root t)))

(defun blackdog-telemetry-open-child-artifacts (&optional root actor)
  "Open one live child artifact directory for ROOT and ACTOR."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (status (blackdog-telemetry-supervisor-status root actor))
         (snapshot (blackdog-snapshot root t))
         (children (blackdog-telemetry--child-rows status snapshot root)))
    (unless children
      (user-error "No live child artifact directories are available"))
    (let* ((candidates
            (mapcar (lambda (row)
                      (cons (format "%s %s"
                                    (alist-get 'task_id row)
                                    (alist-get 'title row))
                            row))
                    children))
           (choice (completing-read "Child artifacts: " candidates nil t))
           (row (cdr (assoc choice candidates)))
           (target (alist-get 'artifact_dir row)))
      (unless target
        (user-error "Selected child has no artifact directory"))
      (blackdog-open-href target nil root t))))

(defun blackdog-telemetry-refresh ()
  "Refresh the current telemetry buffer."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (actor (blackdog-telemetry--actor))
         (session (blackdog-telemetry-session-summary))
         (status-result (condition-case err
                            (cons 'ok (blackdog-telemetry-supervisor-status root actor))
                          (error (cons 'error (error-message-string err)))))
         (snapshot-force
          (or (process-live-p blackdog-telemetry-supervisor-process)
              (and (eq (car status-result) 'ok)
                   (let* ((latest-run (alist-get 'latest_run (cdr status-result)))
                          (status (or (alist-get 'status latest-run)
                                      (alist-get 'final_status latest-run)
                                      "")))
                     (member status '("running" "draining"))))))
         (snapshot-result (condition-case err
                              (cons 'ok (blackdog-snapshot root snapshot-force))
                            (error (cons 'error (error-message-string err)))))
         (recover-result (condition-case err
                             (cons 'ok (blackdog-telemetry-supervisor-recover root actor))
                           (error (cons 'error (error-message-string err)))))
         (report-result (condition-case err
                            (cons 'ok (blackdog-telemetry-supervisor-report root actor))
                          (error (cons 'error (error-message-string err)))))
         (children
          (if (and (eq (car snapshot-result) 'ok)
                   (eq (car status-result) 'ok))
              (blackdog-telemetry--child-rows
               (cdr status-result)
               (cdr snapshot-result)
               root)
            nil))
         (point-before (point))
         (inhibit-read-only t))
    (erase-buffer)
    (setq-local blackdog-buffer-root root)
    (setq-local blackdog-telemetry-actor actor)
    (setq-local blackdog-telemetry-last-status
                (and (eq (car status-result) 'ok) (cdr status-result)))
    (blackdog-telemetry--insert-controls status-result children)
    (blackdog-telemetry--insert-session session)
    (blackdog-telemetry--insert-supervisor-status status-result)
    (blackdog-telemetry--insert-live-children children)
    (blackdog-telemetry--insert-live-output children)
    (blackdog-telemetry--insert-process-output)
    (blackdog-telemetry--insert-supervisor-recover recover-result)
    (blackdog-telemetry--insert-supervisor-report report-result)
    (goto-char (min point-before (point-max)))
    (blackdog-telemetry--ensure-auto-refresh
     (blackdog-telemetry--should-auto-refresh-p status-result))))

(defun blackdog-telemetry--should-auto-refresh-p (status-result)
  "Return non-nil when STATUS-RESULT should keep auto-refresh enabled."
  (or (process-live-p blackdog-telemetry-supervisor-process)
      (and (eq (car status-result) 'ok)
           (let* ((latest-run (alist-get 'latest_run (cdr status-result)))
                  (status (or (alist-get 'status latest-run)
                              (alist-get 'final_status latest-run)
                              "")))
             (member status '("running" "draining"))))))

(defun blackdog-telemetry--ensure-auto-refresh (enabled)
  "Ensure the current telemetry buffer auto-refreshes when ENABLED is non-nil."
  (cond
   ((not enabled)
    (when (timerp blackdog-telemetry-refresh-timer)
      (cancel-timer blackdog-telemetry-refresh-timer)
      (setq blackdog-telemetry-refresh-timer nil)))
   ((or (not blackdog-telemetry-auto-refresh-interval)
        (<= blackdog-telemetry-auto-refresh-interval 0))
    nil)
   ((timerp blackdog-telemetry-refresh-timer)
    nil)
   (t
    (let ((buffer (current-buffer)))
      (setq-local
       blackdog-telemetry-refresh-timer
       (run-at-time
        blackdog-telemetry-auto-refresh-interval
        blackdog-telemetry-auto-refresh-interval
        (lambda ()
          (if (buffer-live-p buffer)
              (with-current-buffer buffer
                (blackdog-telemetry-refresh))
            (when (timerp blackdog-telemetry-refresh-timer)
              (cancel-timer blackdog-telemetry-refresh-timer)
              (setq blackdog-telemetry-refresh-timer nil))))))))))

(defun blackdog-telemetry--insert-controls (status-result children)
  "Insert control buttons derived from STATUS-RESULT and CHILDREN."
  (let* ((actor (blackdog-telemetry--actor))
         (root (or blackdog-buffer-root (blackdog-project-root)))
         (latest-run (and (eq (car status-result) 'ok)
                          (alist-get 'latest_run (cdr status-result))))
         (run-dir (alist-get 'run_dir latest-run))
         (process-status (blackdog-telemetry--local-process-status)))
    (insert "Supervisor Controls\n")
    (insert (format "Actor: %s\n" actor))
    (insert (format "Local Process: %s\n" process-status))
    (when run-dir
      (insert (format "Latest Run Dir: %s\n" run-dir)))
    (blackdog-telemetry--insert-action-button
     "Start Supervisor"
     (lambda (_button)
       (blackdog-telemetry-start-supervisor root actor)))
    (insert "  ")
    (blackdog-telemetry--insert-action-button
     "Stop After Current Tasks"
     (lambda (_button)
       (blackdog-telemetry-stop-supervisor root actor)))
    (insert "  ")
    (blackdog-telemetry--insert-action-button
     "Open Runs"
     (lambda (_button)
       (blackdog-telemetry-open-runs root)))
    (when run-dir
      (insert "  ")
      (blackdog-telemetry--insert-action-button
       "Open Latest Run"
       (lambda (_button)
         (blackdog-open-href run-dir nil root t))))
    (when children
      (insert "  ")
      (blackdog-telemetry--insert-action-button
       "Open Child Artifacts"
       (lambda (_button)
         (blackdog-telemetry-open-child-artifacts root actor))))
    (insert "\n\n")))

(defun blackdog-telemetry--local-process-status ()
  "Return a readable status label for the local supervisor process."
  (cond
   ((process-live-p blackdog-telemetry-supervisor-process)
    (format "running (pid %s)"
            (process-id blackdog-telemetry-supervisor-process)))
   ((processp blackdog-telemetry-supervisor-process)
    (format "%s" (process-status blackdog-telemetry-supervisor-process)))
   (t
    "not started from this buffer")))

(defun blackdog-telemetry--insert-action-button (label action)
  "Insert one action button with LABEL bound to ACTION."
  (insert-text-button label 'follow-link t 'action action))

(defun blackdog-telemetry--insert-session (session)
  "Insert Emacs SESSION telemetry."
  (insert "Session Telemetry\n")
  (insert (format "Total CLI Calls: %s\n" (alist-get 'total_calls session 0)))
  (insert (format "Failed CLI Calls: %s\n" (alist-get 'failed_calls session 0)))
  (when-let ((last-call (alist-get 'last_call session)))
    (insert (format "Last Call: %s  status=%s  %.1f ms  %s\n"
                    (alist-get 'label last-call)
                    (alist-get 'status last-call)
                    (* 1000.0 (alist-get 'duration last-call 0.0))
                    (alist-get 'at last-call))))
  (when-let ((last-error (alist-get 'last_error session)))
    (insert (format "Last Error: %s  status=%s  %s\n"
                    (alist-get 'label last-error)
                    (alist-get 'status last-error)
                    (alist-get 'at last-error)))
    (insert (format "  %s\n" (alist-get 'message last-error))))
  (insert "\nCommand Stats\n")
  (insert (format "%-18s %5s %5s %9s %9s %6s\n"
                  "Command" "Count" "Fail" "Avg ms" "Last ms" "Code"))
  (dolist (row (alist-get 'commands session))
    (insert (format "%-18s %5s %5s %9.1f %9.1f %6s\n"
                    (alist-get 'label row)
                    (alist-get 'count row 0)
                    (alist-get 'failures row 0)
                    (* 1000.0 (alist-get 'average_duration row 0.0))
                    (* 1000.0 (alist-get 'last_duration row 0.0))
                    (alist-get 'last_status row 0))))
  (insert "\n"))

(defun blackdog-telemetry--insert-supervisor-status (status-result)
  "Insert STATUS-RESULT into the current telemetry buffer."
  (insert "Supervisor Status\n")
  (pcase (car status-result)
    ('error
     (insert (format "Unable to load supervisor status: %s\n\n" (cdr status-result))))
    ('ok
     (let* ((status (cdr status-result))
            (latest-run (alist-get 'latest_run status))
            (ready-tasks (alist-get 'ready_tasks status))
            (recent-results (alist-get 'recent_results status))
            (control-messages (alist-get 'open_control_messages status))
            (last-step (alist-get 'last_step latest-run)))
       (insert (format "Actor: %s\n" (alist-get 'actor status)))
       (when latest-run
         (insert (format "Latest Run: %s  %s  checked=%s\n"
                         (alist-get 'run_id latest-run)
                         (or (alist-get 'final_status latest-run)
                             (alist-get 'status latest-run))
                         (or (alist-get 'last_checked_at latest-run) ""))))
       (when last-step
         (insert (format "Last Step: %s  running=%s  ready=%s\n"
                         (or (alist-get 'status last-step) "")
                         (length (alist-get 'running_task_ids last-step))
                         (length (alist-get 'ready_task_ids last-step)))))
       (insert (format "Ready Tasks: %s  Recent Results: %s  Open Controls: %s\n"
                       (length ready-tasks)
                       (length recent-results)
                       (length control-messages)))
       (when ready-tasks
         (insert "Ready Queue\n")
         (dolist (row ready-tasks)
           (insert (format "- %s  %s\n"
                           (alist-get 'id row)
                           (alist-get 'title row)))))
       (when recent-results
         (insert "Recent Result Actors\n")
         (dolist (row (seq-take recent-results 5))
           (insert (format "- [%s] %s  %s\n"
                           (alist-get 'status row)
                           (alist-get 'task_id row)
                           (alist-get 'actor row)))))
       (insert "\n")))))

(defun blackdog-telemetry--child-rows (status snapshot root)
  "Return live child rows derived from STATUS, SNAPSHOT, and ROOT."
  (let* ((latest-run (alist-get 'latest_run status))
         (last-step (alist-get 'last_step latest-run))
         (running-task-ids (alist-get 'running_task_ids last-step))
         (recent-task-ids (mapcar (lambda (row)
                                    (alist-get 'task_id row))
                                  (alist-get 'recent_results status)))
         (task-ids (seq-uniq
                    (delq nil
                          (append running-task-ids recent-task-ids)))))
    (delq nil
          (mapcar (lambda (task-id)
                    (blackdog-telemetry--child-row task-id latest-run snapshot root))
                  task-ids))))

(defun blackdog-telemetry--child-row (task-id latest-run snapshot root)
  "Build one child row for TASK-ID from LATEST-RUN, SNAPSHOT, and ROOT."
  (let* ((task (blackdog-task-by-id task-id snapshot))
         (artifact-dir
          (or (blackdog-resolve-href (alist-get 'run_dir_href task) snapshot root)
              (when-let ((run-dir (alist-get 'run_dir latest-run)))
                (expand-file-name task-id run-dir)))))
    (when artifact-dir
      `((task_id . ,task-id)
        (title . ,(or (alist-get 'title task) ""))
        (artifact_dir . ,artifact-dir)
        (prompt . ,(blackdog-telemetry--child-artifact-path task latest-run snapshot root "prompt_href" artifact-dir "prompt.txt"))
        (stdout . ,(blackdog-telemetry--child-artifact-path task latest-run snapshot root "stdout_href" artifact-dir "stdout.log"))
        (stderr . ,(blackdog-telemetry--child-artifact-path task latest-run snapshot root "stderr_href" artifact-dir "stderr.log"))
        (metadata . ,(blackdog-telemetry--child-artifact-path task latest-run snapshot root "metadata_href" artifact-dir "metadata.json"))
        (diff . ,(blackdog-telemetry--child-artifact-path task latest-run snapshot root "diff_href" artifact-dir "changes.diff"))))))

(defun blackdog-telemetry--child-artifact-path (task _latest-run snapshot root key artifact-dir filename)
  "Return absolute path for TASK KEY or ARTIFACT-DIR/FILENAME."
  (or (blackdog-resolve-href (alist-get (intern key) task) snapshot root)
      (let ((path (expand-file-name filename artifact-dir)))
        (and (file-exists-p path) path))))

(defun blackdog-telemetry--insert-live-children (children)
  "Insert CHILDREN artifact rows into the current telemetry buffer."
  (insert "Latest Child Artifacts\n")
  (if children
      (dolist (row children)
        (insert (format "- %s  %s\n"
                        (alist-get 'task_id row)
                        (alist-get 'title row)))
        (insert "  Links: ")
        (blackdog-telemetry--insert-link-buttons row)
        (insert "\n")
        (insert (format "  Directory: %s\n\n" (alist-get 'artifact_dir row))))
    (insert "No live child artifact directories detected.\n\n")))

(defun blackdog-telemetry--insert-link-buttons (row)
  "Insert artifact link buttons for one child ROW."
  (let ((links `(("Artifacts" . ,(alist-get 'artifact_dir row))
                 ("Prompt" . ,(alist-get 'prompt row))
                 ("Stdout" . ,(alist-get 'stdout row))
                 ("Stderr" . ,(alist-get 'stderr row))
                 ("Metadata" . ,(alist-get 'metadata row))
                 ("Diff" . ,(alist-get 'diff row)))))
    (dolist (link links)
      (when-let ((target (cdr link)))
        (blackdog-telemetry--insert-action-button
         (car link)
         (lambda (_button)
           (blackdog-open-href target nil blackdog-buffer-root t)))
        (insert " ")))))

(defun blackdog-telemetry--insert-live-output (children)
  "Insert live child output tails for CHILDREN."
  (insert "Live Child Output\n")
  (if children
      (dolist (row children)
        (insert (format "%s  %s\n"
                        (alist-get 'task_id row)
                        (alist-get 'title row)))
        (blackdog-telemetry--insert-tail "stderr.log" (alist-get 'stderr row))
        (blackdog-telemetry--insert-tail "stdout.log" (alist-get 'stdout row))
        (insert "\n"))
    (insert "No live child output is available.\n\n")))

(defun blackdog-telemetry--insert-tail (label path)
  "Insert a tailed view for LABEL from PATH."
  (insert (format "%s\n" label))
  (let ((tail (blackdog-telemetry--tail-path path)))
    (if (and tail (not (string-empty-p tail)))
        (insert tail "\n")
      (insert "(no output)\n"))))

(defun blackdog-telemetry--tail-path (path)
  "Return a trimmed tail string for PATH."
  (when (and path (file-readable-p path))
    (with-temp-buffer
      (let* ((size (file-attribute-size (file-attributes path)))
             (start (max 0 (- size blackdog-telemetry-log-tail-bytes))))
        (insert-file-contents path nil start size))
      (let* ((lines (split-string (buffer-string) "\n"))
             (tail (last lines (min (length lines) blackdog-telemetry-log-tail-lines))))
        (string-trim-right (mapconcat #'identity tail "\n"))))))

(defun blackdog-telemetry--insert-process-output ()
  "Insert the local supervisor process stdout and stderr tails."
  (insert "Supervisor Process Output\n")
  (blackdog-telemetry--insert-buffer-tail "stdout" blackdog-telemetry-supervisor-output-buffer)
  (blackdog-telemetry--insert-buffer-tail "stderr" blackdog-telemetry-supervisor-error-buffer)
  (insert "\n"))

(defun blackdog-telemetry--insert-buffer-tail (label buffer)
  "Insert LABEL tail from BUFFER."
  (insert (format "%s\n" label))
  (let ((tail (blackdog-telemetry--tail-buffer buffer)))
    (if (and tail (not (string-empty-p tail)))
        (insert tail "\n")
      (insert "(no output)\n"))))

(defun blackdog-telemetry--tail-buffer (buffer)
  "Return the trailing text for BUFFER."
  (when (buffer-live-p buffer)
    (with-current-buffer buffer
      (let* ((lines (split-string (buffer-string) "\n"))
             (tail (last lines (min (length lines) blackdog-telemetry-log-tail-lines))))
        (string-trim-right (mapconcat #'identity tail "\n"))))))

(defun blackdog-telemetry--insert-supervisor-recover (recover-result)
  "Insert RECOVER-RESULT into the current telemetry buffer."
  (insert "Supervisor Recover\n")
  (pcase (car recover-result)
    ('error
     (insert (format "Unable to load supervisor recovery: %s\n\n" (cdr recover-result))))
    ('ok
     (let ((cases (alist-get 'recoverable_cases (cdr recover-result))))
       (if cases
           (dolist (row cases)
             (insert (format "- %s [%s] %s\n"
                             (alist-get 'task_id row)
                             (alist-get 'severity row)
                             (alist-get 'summary row)))
             (when-let ((artifact-dir (alist-get 'child_artifact_dir row)))
               (insert "  ")
               (blackdog-telemetry--insert-action-button
                "Open Child Artifacts"
                (lambda (_button)
                  (blackdog-open-href artifact-dir nil blackdog-buffer-root t)))
               (insert "\n"))
             (when-let ((actions (alist-get 'next_actions row)))
               (insert (format "  Actions: %s\n" (string-join actions ", "))))
             (insert "\n"))
         (insert "No recoverable supervisor cases detected.\n\n"))))))

(defun blackdog-telemetry--insert-supervisor-report (report-result)
  "Insert REPORT-RESULT into the current telemetry buffer."
  (insert "Supervisor Report\n")
  (pcase (car report-result)
    ('error
     (insert (format "Unable to load supervisor report: %s\n" (cdr report-result))))
    ('ok
     (let* ((report (cdr report-result))
            (summary (alist-get 'summary report))
            (startup (alist-get 'startup summary))
            (retry (alist-get 'retry summary))
            (output-shape (alist-get 'output_shape summary))
            (landing (alist-get 'landing summary))
            (observations (alist-get 'observations report)))
       (insert (format "Runs Total: %s\n" (alist-get 'runs_total summary 0)))
       (insert (format "Startup: launched=%s attempts=%s failures=%s success=%.2f\n"
                       (alist-get 'launched startup 0)
                       (alist-get 'attempts startup 0)
                       (alist-get 'launch_failures startup 0)
                       (alist-get 'launch_success_rate startup 0.0)))
       (insert (format "Retry: total=%s retried=%s\n"
                       (alist-get 'retry_total retry 0)
                       (alist-get 'retried_tasks retry 0)))
       (insert (format "Output Shape: complete=%s incomplete=%s rate=%.2f\n"
                       (alist-get 'artifact_complete_attempts output-shape 0)
                       (alist-get 'artifact_incomplete_attempts output-shape 0)
                       (alist-get 'artifact_completion_rate output-shape 0.0)))
       (insert (format "Landing: landed=%s errors=%s success=%.2f\n"
                       (alist-get 'landed_attempts landing 0)
                       (alist-get 'land_error_count landing 0)
                       (alist-get 'landing_success_rate landing 0.0)))
       (when observations
         (insert "Observations\n")
         (dolist (row observations)
           (insert (format "- [%s/%s] %s\n"
                           (alist-get 'category row)
                           (alist-get 'severity row)
                           (alist-get 'summary row)))))
       (insert "\n")))))

(provide 'blackdog-telemetry)

;;; blackdog-telemetry.el ends here
