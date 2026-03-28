;;; blackdog-runs.el --- Run artifact browser for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Tabulated supervisor-run browser backed by `blackdog snapshot`.

;;; Code:

(require 'blackdog-core)
(require 'blackdog-task)
(require 'seq)
(require 'subr-x)
(require 'tabulated-list)

(defvar blackdog-runs-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map tabulated-list-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "RET") #'blackdog-runs-open-run)
    (define-key map (kbd "t") #'blackdog-runs-open-task)
    map)
  "Keymap for `blackdog-runs-mode'.")

(define-derived-mode blackdog-runs-mode tabulated-list-mode "Blackdog-Runs"
  "List Blackdog task runs."
  (setq tabulated-list-format
        [("Task" 14 t)
         ("Run" 24 t)
         ("Status" 10 t)
         ("Actor" 18 t)
         ("Recorded" 25 t)])
  (setq tabulated-list-padding 2)
  (tabulated-list-init-header))

(defun blackdog-runs-open (&optional root)
  "Open the Blackdog run browser for ROOT."
  (interactive)
  (let ((buffer (get-buffer-create "*Blackdog Runs*")))
    (with-current-buffer buffer
      (blackdog-runs-mode)
      (setq-local blackdog-buffer-root (or root (blackdog-project-root)))
      (setq-local blackdog-refresh-function #'blackdog-runs-refresh)
      (blackdog-runs-refresh))
    (pop-to-buffer buffer)))

(defun blackdog-runs-refresh ()
  "Refresh the current run browser."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (snapshot (blackdog-snapshot root))
         (tasks (seq-filter
                 (lambda (task)
                   (and (alist-get 'run_dir_href task)))
                 (alist-get 'tasks snapshot))))
    (setq tabulated-list-entries
          (mapcar (lambda (task)
                    (list
                     (alist-get 'id task)
                     (vector
                      (or (alist-get 'id task) "")
                      (or (blackdog-runs--run-id task) "")
                      (or (alist-get 'latest_result_status task) "running")
                      (or (alist-get 'latest_result_actor task) "")
                      (or (alist-get 'latest_result_at task) ""))))
                  (sort tasks
                        (lambda (left right)
                          (string-greaterp
                           (or (blackdog-runs--run-id left) "")
                           (blackdog-runs--run-id right))))))
    (tabulated-list-print t)))

(defun blackdog-runs-open-run ()
  "Open the run artifact directory for the selected task."
  (interactive)
  (when-let* ((task-id (tabulated-list-get-id))
              (task (blackdog-task-by-id task-id nil blackdog-buffer-root))
              (href (alist-get 'run_dir_href task)))
    (blackdog-open-href href nil blackdog-buffer-root t)))

(defun blackdog-runs-open-task ()
  "Open the selected run's task reader."
  (interactive)
  (when-let* ((task-id (tabulated-list-get-id))
              (task (blackdog-task-by-id task-id nil blackdog-buffer-root)))
    (blackdog-task-view task blackdog-buffer-root)))

(defun blackdog-runs--run-id (task)
  "Return the run identifier for TASK."
  (let ((run-dir (alist-get 'run_dir_href task)))
    (when (and run-dir (not (string-empty-p run-dir)))
      (file-name-nondirectory (directory-file-name run-dir)))))

(provide 'blackdog-runs)

;;; blackdog-runs.el ends here
