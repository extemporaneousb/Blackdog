;;; blackdog-results.el --- Result browser for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Tabulated result list backed by `blackdog snapshot`.

;;; Code:

(require 'blackdog-core)
(require 'blackdog-task)
(require 'tabulated-list)
(require 'subr-x)

(defvar blackdog-results-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map tabulated-list-mode-map)
    (define-key map (kbd "g") #'blackdog-results-refresh)
    (define-key map (kbd "RET") #'blackdog-results-visit)
    (define-key map (kbd "f") #'blackdog-results-open-result)
    map)
  "Keymap for `blackdog-results-mode'.")

(define-derived-mode blackdog-results-mode tabulated-list-mode "Blackdog-Results"
  "List Blackdog task results."
  (setq tabulated-list-format
        [("Task" 14 t)
         ("Status" 10 t)
         ("Actor" 20 t)
         ("Recorded" 25 t)
         ("Title" 0 t)])
  (setq tabulated-list-padding 2)
  (tabulated-list-init-header))

(defun blackdog-results-open (&optional root)
  "Open the Blackdog results buffer for ROOT."
  (interactive)
  (let ((buffer (get-buffer-create "*Blackdog Results*")))
    (with-current-buffer buffer
      (blackdog-results-mode)
      (setq-local blackdog-buffer-root (or root (blackdog-project-root)))
      (setq-local blackdog-refresh-function #'blackdog-results-refresh)
      (blackdog-results-refresh))
    (pop-to-buffer buffer)))

(defun blackdog-results-refresh ()
  "Refresh the results listing."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (snapshot (blackdog-snapshot root t))
         (tasks (seq-filter (lambda (task)
                              (alist-get 'latest_result_status task))
                            (alist-get 'tasks snapshot))))
    (setq tabulated-list-entries
          (mapcar
           (lambda (task)
             (list
              (alist-get 'id task)
              (vector
               (alist-get 'id task)
               (or (alist-get 'latest_result_status task) "")
               (or (alist-get 'latest_result_actor task) "")
               (or (alist-get 'latest_result_at task) "")
               (alist-get 'title task))))
           (sort tasks
                 (lambda (left right)
                   (string-greaterp
                    (or (alist-get 'latest_result_at left) "")
                    (or (alist-get 'latest_result_at right) ""))))))
    (tabulated-list-print t)))

(defun blackdog-results-visit ()
  "Visit the task reader for the current result row."
  (interactive)
  (when-let* ((task-id (tabulated-list-get-id))
              (task (blackdog-task-by-id task-id nil blackdog-buffer-root)))
    (blackdog-task-view task blackdog-buffer-root)))

(defun blackdog-results-open-result ()
  "Open the latest result file for the current row."
  (interactive)
  (when-let* ((task-id (tabulated-list-get-id))
              (task (blackdog-task-by-id task-id nil blackdog-buffer-root))
              (href (alist-get 'latest_result_href task)))
    (blackdog-open-href href nil blackdog-buffer-root t)))

(provide 'blackdog-results)

;;; blackdog-results.el ends here
