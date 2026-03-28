;;; blackdog-telemetry.el --- Supervisor telemetry and package instrumentation -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Read-only telemetry surface for session-local Emacs instrumentation plus
;; Blackdog supervisor status/report JSON.

;;; Code:

(require 'blackdog-core)
(require 'subr-x)

(defcustom blackdog-telemetry-supervisor-actor "supervisor/emacs"
  "Supervisor actor to inspect from Emacs."
  :type 'string
  :group 'blackdog)

(defvar blackdog-telemetry-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "c") #'blackdog-telemetry-clear-session)
    map)
  "Keymap for `blackdog-telemetry-mode'.")

(define-derived-mode blackdog-telemetry-mode special-mode "Blackdog-Telemetry"
  "Read-only telemetry buffer for Blackdog.")

(defun blackdog-telemetry-supervisor-status (&optional root actor)
  "Return supervisor status JSON for ROOT and ACTOR."
  (blackdog--call-json
   (or root (blackdog-project-root))
   "supervise" "status"
   "--actor" (or actor blackdog-telemetry-supervisor-actor)
   "--format" "json"))

(defun blackdog-telemetry-supervisor-report (&optional root actor)
  "Return supervisor report JSON for ROOT and ACTOR."
  (blackdog--call-json
   (or root (blackdog-project-root))
   "supervise" "report"
   "--actor" (or actor blackdog-telemetry-supervisor-actor)
   "--format" "json"))

(defun blackdog-telemetry-open (&optional root)
  "Open the Blackdog telemetry buffer for ROOT."
  (interactive)
  (let ((buffer (get-buffer-create "*Blackdog Telemetry*")))
    (with-current-buffer buffer
      (blackdog-telemetry-mode)
      (setq-local blackdog-buffer-root (or root (blackdog-project-root)))
      (setq-local blackdog-refresh-function #'blackdog-telemetry-refresh)
      (blackdog-telemetry-refresh))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-telemetry-clear-session ()
  "Clear Emacs-side telemetry counters and refresh the buffer."
  (interactive)
  (blackdog-clear-telemetry)
  (blackdog-telemetry-refresh))

(defun blackdog-telemetry-refresh ()
  "Refresh the current telemetry buffer."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (session (blackdog-telemetry-session-summary))
         (status-result (condition-case err
                            (cons 'ok (blackdog-telemetry-supervisor-status root))
                          (error (cons 'error (error-message-string err)))))
         (report-result (condition-case err
                            (cons 'ok (blackdog-telemetry-supervisor-report root))
                          (error (cons 'error (error-message-string err)))))
         (inhibit-read-only t))
    (erase-buffer)
    (setq-local blackdog-buffer-root root)
    (blackdog-telemetry--insert-session session)
    (blackdog-telemetry--insert-supervisor-status status-result)
    (blackdog-telemetry--insert-supervisor-report report-result)
    (goto-char (point-min))))

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
            (recent-results (alist-get 'recent_results status)))
       (insert (format "Actor: %s\n" (alist-get 'actor status)))
       (when latest-run
         (insert (format "Latest Run: %s  %s  checked=%s\n"
                         (alist-get 'run_id latest-run)
                         (or (alist-get 'final_status latest-run)
                             (alist-get 'status latest-run))
                         (or (alist-get 'last_checked_at latest-run) ""))))
       (insert (format "Ready Tasks: %s  Recent Results: %s  Open Controls: %s\n"
                       (length ready-tasks)
                       (length recent-results)
                       (length (alist-get 'open_control_messages status))))
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
