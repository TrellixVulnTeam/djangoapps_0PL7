###
# Copyright (C) 2014-2018 Taiga Agile LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# File: history/history.directive.coffee
###

module = angular.module('taigaHistory')

bindOnce = @.taiga.bindOnce

HistorySectionDirective = () ->
    link = (scope, el, attr, ctrl) ->
        scope.$on "object:updated", -> ctrl._loadActivity()

        scope.$watch 'vm.id', (value) ->
            ctrl._loadHistory()

    return {
        link: link,
        templateUrl:"history/history.html",
        controller: "HistorySection",
        controllerAs: "vm",
        bindToController: true,
        scope: {
            type: "=",
            name: "@",
            id: "=",
            project: "="
        }
    }

HistorySectionDirective.$inject = []

module.directive("tgHistorySection", HistorySectionDirective)
