;;; blackdog-core.el --- Core helpers for Blackdog Emacs integration -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;; Author: Blackdog contributors
;; Keywords: tools, vc

;;; Commentary:

;; Core process, JSON, cache, and path helpers for the Blackdog Emacs package.

;;; Code:

(require 'json)
(require 'seq)
(require 'subr-x)

(defgroup blackdog nil
  "Operate Blackdog from Emacs."
  :group 'tools
  :prefix "blackdog-")

(defcustom blackdog-default-command nil
  "Absolute path to the `blackdog' CLI.

When nil, resolve `./.VE/bin/blackdog' from the current project root and
fall back to `blackdog' on PATH."
  :type '(choice (const :tag "Auto-detect" nil) file)
  :group 'blackdog)

(defvar blackdog--snapshot-cache (make-hash-table :test #'equal))
(defvar blackdog--telemetry-command-table (make-hash-table :test #'equal))
(defvar blackdog--telemetry-total-calls 0)
(defvar blackdog--telemetry-failed-calls 0)
(defvar blackdog--telemetry-last-call nil)
(defvar blackdog--telemetry-last-error nil)
(defvar-local blackdog-buffer-root nil)
(defvar-local blackdog-refresh-function nil)

(defun blackdog-project-root (&optional dir)
  "Return the Blackdog project root above DIR or `default-directory'."
  (let ((root (locate-dominating-file (or dir default-directory) "blackdog.toml")))
    (unless root
      (user-error "No blackdog.toml found above %s" (or dir default-directory)))
    (directory-file-name (expand-file-name root))))

(defun blackdog-command (&optional root)
  "Return the Blackdog CLI path for ROOT."
  (or blackdog-default-command
      (let ((candidate (expand-file-name ".VE/bin/blackdog"
                                         (or root (blackdog-project-root)))))
        (cond
         ((file-executable-p candidate) candidate)
         ((executable-find "blackdog"))
         (t (user-error "Could not find a Blackdog CLI for %s"
                        (or root (blackdog-project-root))))))))

(defun blackdog--call (root &rest args)
  "Run Blackdog in ROOT with ARGS and return stdout."
  (let ((default-directory root)
        (buffer (generate-new-buffer " *blackdog*"))
        (started-at (float-time))
        status
        output)
    (unwind-protect
        (progn
          (setq status (apply #'process-file (blackdog-command root) nil buffer nil args))
          (with-current-buffer buffer
            (setq output (buffer-string)))
          (blackdog--record-telemetry root args status output (- (float-time) started-at))
          (if (zerop status)
              output
            (error "blackdog %s failed (%s): %s"
                   (string-join args " ")
                   status
                   (string-trim output))))
      (kill-buffer buffer))))

(defun blackdog--call-json (root &rest args)
  "Run Blackdog in ROOT with ARGS and parse the JSON response."
  (let ((json-object-type 'alist)
        (json-array-type 'list)
        (json-key-type 'symbol)
        (json-false :json-false)
        (json-null nil))
    (json-read-from-string
     (string-trim (apply #'blackdog--call root args)))))

(defun blackdog-clear-cache (&optional root)
  "Clear the cached snapshot for ROOT, or every cached snapshot when ROOT is nil."
  (if root
      (remhash (expand-file-name root) blackdog--snapshot-cache)
    (clrhash blackdog--snapshot-cache)))

(defun blackdog-snapshot (&optional root force)
  "Return the current Blackdog snapshot for ROOT.

When FORCE is non-nil, refresh the cached snapshot."
  (let* ((root (expand-file-name (or root (blackdog-project-root))))
         (cached (and (not force) (gethash root blackdog--snapshot-cache))))
    (or cached
        (let ((snapshot (blackdog--call-json root "snapshot")))
          (puthash root snapshot blackdog--snapshot-cache)
          snapshot))))

(defun blackdog-control-dir (&optional snapshot root)
  "Return the Blackdog control dir from SNAPSHOT or ROOT."
  (alist-get 'control_dir (or snapshot (blackdog-snapshot root))))

(defun blackdog-task-by-id (task-id &optional snapshot root)
  "Return TASK-ID from SNAPSHOT or ROOT."
  (seq-find (lambda (task)
              (equal task-id (alist-get 'id task)))
            (alist-get 'tasks (or snapshot (blackdog-snapshot root)))))

(defun blackdog-task-candidates (&optional snapshot root)
  "Return completion candidates for tasks from SNAPSHOT or ROOT."
  (mapcar
   (lambda (task)
     (cons (format "[%s] %s %s"
                   (or (alist-get 'operator_status task)
                       (alist-get 'status task)
                       "?")
                   (alist-get 'id task)
                   (alist-get 'title task))
           task))
   (alist-get 'tasks (or snapshot (blackdog-snapshot root)))))

(defun blackdog-read-task (&optional prompt snapshot root)
  "Read one task from SNAPSHOT or ROOT with PROMPT."
  (let* ((snapshot (or snapshot (blackdog-snapshot root t)))
         (candidates (blackdog-task-candidates snapshot))
         (choice (completing-read (or prompt "Task: ") candidates nil t)))
    (cdr (assoc choice candidates))))

(defun blackdog-resolve-href (href &optional snapshot root)
  "Resolve relative HREF against the control dir from SNAPSHOT or ROOT."
  (when (and href (not (string-empty-p href)))
    (cond
     ((string-match-p "\\`https?://" href) href)
     ((file-name-absolute-p href) href)
     (t (expand-file-name href (blackdog-control-dir snapshot root))))))

(defun blackdog-resolve-project-path (path &optional root)
  "Resolve PATH relative to ROOT."
  (when (and path (not (string-empty-p path)))
    (if (file-name-absolute-p path)
        path
      (expand-file-name path (or root (blackdog-project-root))))))

(defun blackdog-open-href (href &optional snapshot root other-window)
  "Open HREF using SNAPSHOT or ROOT.

When OTHER-WINDOW is non-nil, use another window for file paths."
  (let ((target (blackdog-resolve-href href snapshot root)))
    (unless target
      (user-error "No artifact path available"))
    (if (string-match-p "\\`https?://" target)
        (browse-url target)
      (funcall (if other-window #'find-file-other-window #'find-file) target))))

(defun blackdog-open-project-path (path &optional root other-window)
  "Open project PATH relative to ROOT.

When OTHER-WINDOW is non-nil, use another window."
  (let ((target (blackdog-resolve-project-path path root)))
    (unless target
      (user-error "No project path available"))
    (funcall (if other-window #'find-file-other-window #'find-file) target)))

(defun blackdog-refresh (&optional root)
  "Refresh the current Blackdog buffer and clear cached data for ROOT."
  (interactive)
  (let ((root (or root blackdog-buffer-root (blackdog-project-root))))
    (blackdog-clear-cache root)
    (when (functionp blackdog-refresh-function)
      (funcall blackdog-refresh-function))))

(defun blackdog-clear-telemetry ()
  "Reset the session-local Emacs telemetry counters."
  (interactive)
  (setq blackdog--telemetry-total-calls 0
        blackdog--telemetry-failed-calls 0
        blackdog--telemetry-last-call nil
        blackdog--telemetry-last-error nil)
  (clrhash blackdog--telemetry-command-table))

(defun blackdog--telemetry-label (args)
  "Return a readable label for Blackdog ARGS."
  (let* ((prefix (seq-take args (min 2 (length args))))
         (filtered (seq-filter (lambda (arg)
                                 (not (string-prefix-p "--" arg)))
                               prefix)))
    (string-join (or filtered prefix) " ")))

(defun blackdog--record-telemetry (root args status output duration)
  "Record one Blackdog CLI telemetry sample for ROOT, ARGS, STATUS, OUTPUT, and DURATION."
  (let* ((label (blackdog--telemetry-label args))
         (row (copy-tree (or (gethash label blackdog--telemetry-command-table)
                             '((count . 0)
                               (failures . 0)
                               (total_duration . 0.0)
                               (last_duration . 0.0)
                               (last_status . 0)
                               (last_at . "")))))
         (at (format-time-string "%FT%T%z")))
    (setf (alist-get 'count row) (1+ (alist-get 'count row 0)))
    (setf (alist-get 'total_duration row)
          (+ (alist-get 'total_duration row 0.0) duration))
    (setf (alist-get 'last_duration row) duration)
    (setf (alist-get 'last_status row) status)
    (setf (alist-get 'last_at row) at)
    (when (not (zerop status))
      (setf (alist-get 'failures row) (1+ (alist-get 'failures row 0))))
    (puthash label row blackdog--telemetry-command-table)
    (setq blackdog--telemetry-total-calls (1+ blackdog--telemetry-total-calls))
    (setq blackdog--telemetry-last-call
          `((label . ,label)
            (root . ,root)
            (status . ,status)
            (duration . ,duration)
            (at . ,at)))
    (when (not (zerop status))
      (setq blackdog--telemetry-failed-calls (1+ blackdog--telemetry-failed-calls))
      (setq blackdog--telemetry-last-error
            `((label . ,label)
              (status . ,status)
              (message . ,(string-trim output))
              (at . ,at))))))

(defun blackdog-telemetry-session-summary ()
  "Return session-local Emacs telemetry as an alist."
  (let (commands)
    (maphash (lambda (label row)
               (push `((label . ,label)
                       (count . ,(alist-get 'count row 0))
                       (failures . ,(alist-get 'failures row 0))
                       (average_duration . ,(/ (alist-get 'total_duration row 0.0)
                                              (max 1 (alist-get 'count row 1))))
                       (last_duration . ,(alist-get 'last_duration row 0.0))
                       (last_status . ,(alist-get 'last_status row 0))
                       (last_at . ,(alist-get 'last_at row "")))
                     commands))
             blackdog--telemetry-command-table)
    `((total_calls . ,blackdog--telemetry-total-calls)
      (failed_calls . ,blackdog--telemetry-failed-calls)
      (last_call . ,blackdog--telemetry-last-call)
      (last_error . ,blackdog--telemetry-last-error)
      (commands . ,(sort commands
                         (lambda (left right)
                           (> (alist-get 'count left 0)
                              (alist-get 'count right 0))))))))

(provide 'blackdog-core)

;;; blackdog-core.el ends here
